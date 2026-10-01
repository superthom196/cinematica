"""The HTTP server: every route the page and the TV app call.
"""
import hmac, ipaddress, json, os, re, shutil, subprocess, sys, tempfile, threading, time, urllib.error, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from providers import contract, gateway
import browser_play
import shelf

import config, core, nowplaying, streams, netprofile, catalogue, torrents, transcode, sendspin, tvlink, jobs, browser_session, watching, channels, admin

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _proxy_upstream(self, url, headers=None):
        """Stream any upstream through this server, Range and framing intact.

        Split out of _proxy_source so the torrent route below can reuse it. The
        framing in particular is not optional anywhere: the local streaming
        server answers some requests chunked too, and a body that goes out under
        keep-alive with no declared length and no terminator makes the player
        read the whole film and then block forever on a connection that will
        never say anything else.
        """
        h = dict(headers or {})
        h.setdefault("User-Agent", config.UA)
        h["Accept-Encoding"] = "identity"
        rng = self.headers.get("Range")
        if rng:
            h["Range"] = rng
        try:
            r = urllib.request.urlopen(
                urllib.request.Request(url, headers=h), timeout=30)
        except urllib.error.HTTPError as e:
            # The upstream's own answer, not ours -- but its body may echo a
            # signed URL back, so it is not forwarded.
            return self._send(e.code if 400 <= e.code < 600 else 502,
                              {"err": "source refused the request"})
        except Exception as e:
            # redact: the message can contain the URL, and the URL can BE the
            # credential (a signed query string). Only the generic shapes
            # (URL userinfo, query secrets, Bearer, JWT) are caught here --
            # the literal-secret pass lives behind the registry, which
            # server.py never imports directly; gateway.py is the seam for
            # that, and this path is a plain network error, not a call
            # through it.
            return self._send(502, {"err": contract.redact(
                "%s: %s" % (type(e).__name__, e), gateway.secret_values())})
        try:
            status = getattr(r, "status", 200) or 200
            self.send_response(status)
            for name in ("Content-Type", "Content-Length", "Content-Range",
                         "Accept-Ranges", "Last-Modified", "ETag"):
                v = r.headers.get(name)
                if v:
                    self.send_header(name, v)
            if not r.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")

            # How the player is to know the body has ended.
            #
            # Forwarding the headers above is not enough on its own. When the
            # upstream answers with Transfer-Encoding: chunked, http.client
            # has already consumed that framing by the time `r` reaches here,
            # and it does not invent a Content-Length it was never sent -- so
            # the loop above copies no length header, and this response goes
            # out under HTTP/1.1 keep-alive with no declared length and no
            # terminator. The player reads every byte of the film and then
            # blocks forever on a connection that is never going to say
            # anything else: the hang at completion.
            #
            # So supply framing of our own. Chunked for an HTTP/1.1 client --
            # the zero-length chunk is a positive end-of-body, so a truncated
            # transfer is still distinguishable from a complete one, and the
            # connection survives for the next range request. For anything
            # older, where chunked is not available, hang up instead and let
            # EOF do it.
            # 204 and 304 carry no body at all, by definition; framing one
            # would describe a body that is never sent and hang the player on
            # the very response that was supposed to be cheap.
            chunked = False
            if r.headers.get("Content-Length") is None and status not in (204, 304):
                if self.request_version >= "HTTP/1.1":
                    chunked = True
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.close_connection = True
                    self.send_header("Connection", "close")
            self.end_headers()

            if not chunked:
                shutil.copyfileobj(r, self.wfile, 256 * 1024)
            else:
                try:
                    while True:
                        buf = r.read(256 * 1024)
                        if not buf:
                            break
                        self.wfile.write(b"%x\r\n" % len(buf))
                        self.wfile.write(buf)
                        self.wfile.write(b"\r\n")
                except BaseException:
                    # Stopping mid-message leaves the chunked stream without
                    # its terminator, and the next response parsed off this
                    # connection would be read as a continuation of this one.
                    # Never reuse it.
                    self.close_connection = True
                    raise
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # The player seeked or stopped. Routine, not an error.
            pass
        finally:
            try: r.close()
            except Exception: pass

    def _proxy_source(self, key):
        """A direct source, with its credentials added here and nowhere else."""
        if not contract.RE_HASH40.match(key or ""):
            return self._send(400, {"err": "bad source key"})
        src = streams.source_for(key)
        if not src:
            return self._send(404, {"err": "unknown source"})
        h = dict(src["headers"])
        h.setdefault("User-Agent", config.UA)
        return self._proxy_upstream(src["url"], h)

    def _proxy_torrent(self, ih, idx):
        """The local streaming server, reached through this origin instead.

        The browser player must never be told to open the streaming server's own
        port: the page may have been opened on a tailnet address that has no
        route to it, and a LAN-only host name means nothing to a phone away from
        the house. Same bytes, same Range behaviour, one origin.
        """
        if not contract.RE_HASH40.match(ih or ""):
            return self._send(400, {"err": "bad infoHash"})
        url = "%s/%s" % (config.STREMIO_IN, ih) + (("/%s" % idx) if idx is not None else "")
        return self._proxy_upstream(url)

    def _id(self, raw):
        """A title id out of a URL path.

        Percent-decoded, because a provider-qualified id carries a colon and
        the TV encodes it. Decoding a value with no escapes is a no-op, so this
        is safe on the older bare-numeric ids too.
        """
        return urllib.parse.unquote(raw or "").strip()

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if self.close_connection:
            # Hanging up after this response: say so, rather than leaving a
            # keep-alive client to find out by having its next request reset.
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _retry(self, secs=2):
        """503 + Retry-After: the standard "come back shortly" answer for a
        long-poll that timed out without becoming ready. Kept separate from
        _send() because Retry-After has no place in an ordinary JSON reply."""
        body = json.dumps({"err": "not ready"}).encode()
        self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Retry-After", str(secs))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self, cap=65536):
        """The JSON a client posted, {} if there is none worth reading, or None
        if the client declared a body bigger than the cap.

        Deliberately small: every POST is drained through it now, and a body
        must never be able to make this process hold megabytes per connection.
        None rather than {} for the oversize case because those bytes are NOT
        read off the socket -- the connection is left standing in the middle of a
        message, and the next request parsed from it would be a slice of that
        body. The caller has to answer 413 and hang up."""
        raw = self._read_body_capped(cap)
        if raw is None:
            return None
        if not raw:
            return {}
        try:
            d = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}

    def _host_ok(self):
        """False for a request addressed to a name this server does not answer to.

        This is the DNS-rebinding guard, and it has to run on GET as well as
        POST: once the browser believes the attacker's page and this server
        share an origin, it will read the response back, so /src/ and /audio/
        leak the film just as surely as a forged POST starts one.

        A request with no Host at all is accepted -- that is an HTTP/1.0
        client, and no browser omits it.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return True
        if host.startswith("["):                 # [::1]:8090 -- bracketed IPv6
            host = host[1:host.find("]")] if "]" in host else host[1:]
        elif host.count(":") == 1:               # name:port, never bare IPv6
            host = host.split(":", 1)[0]
        if not host:
            return False
        try:
            ipaddress.ip_address(host)           # LAN address, tailnet address
            return True
        except ValueError:
            pass
        if host == "localhost" or host == config.PUBLIC_HOST.lower():
            return True
        return host in config.HOST_ALLOW or host.endswith(config.HOST_ALLOW_SUFFIX)

    def _origin_ok(self):
        """False for a POST that smells like a cross-site browser request.

        A request with no Origin (curl, the TV app) is always accepted. One
        that has an Origin whose host disagrees with our own Host header, or
        a Sec-Fetch-Site saying "cross-site", is a forgery riding someone
        else's page and is rejected before any route runs.
        """
        origin = self.headers.get("Origin")
        if origin:
            host = urllib.parse.urlsplit(origin).netloc  # scheme stripped, port kept
            if host and host != self.headers.get("Host"):
                return False
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            return False
        return True

    # ---- provider admin --------------------------------------------------------
    def _session_cookie(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == admin.ADMIN_COOKIE and v:
                return v
        return None

    def _require_admin(self):
        """Valid session cookie, and -- for anything but a GET -- a
        X-Cinematica-CSRF header that matches it. Sends a machine-readable
        401 ({"err": "admin required"}) and returns None on failure, so a
        caller just does `if self._require_admin() is None: return` and the
        web UI can show a login form instead of a broken page."""
        tok = self._session_cookie()
        if not tok or not gateway.check_session(tok):
            self._send(401, {"err": "admin required"})
            return None
        if self.command != "GET":
            given = self.headers.get(admin.CSRF_HEADER) or ""
            expected = gateway.csrf_for(tok)
            if not given or not hmac.compare_digest(given, expected):
                self._send(401, {"err": "admin required"})
                return None
        return tok

    def _provider_error(self, ex):
        d = ex.as_dict()
        d["message"] = contract.redact(d.get("message"), gateway.secret_values())
        return self._send(admin.PROVIDER_ERROR_STATUS.get(ex.code, 500), d)

    def _provider_summary(self, rec):
        code, msg = gateway.provider_status(rec.id)
        last_test = rec.last_test
        if isinstance(last_test, dict):
            last_test = dict(last_test)
            if last_test.get("message"):
                last_test["message"] = contract.redact(last_test["message"], gateway.secret_values())
        # A Stremio add-on is configured by its URL, so "source" is not a bare
        # address: the account token and the debrid API key live in a path
        # segment of it. Declared secret config fields have always been masked
        # here; the URL was being handed back verbatim beside them, which made
        # the masking of the fields beside it beside the point. Masked, not
        # dropped -- contract.mask_url keeps the host, so the settings page can
        # still say which add-on this is, and a public add-on with nothing
        # configured still reads in full.
        return {"id": rec.id, "name": rec.manifest.get("name"), "version": rec.manifest.get("version"),
                "capabilities": rec.manifest.get("capabilities"), "enabled": rec.enabled,
                "source": contract.mask_url(rec.source), "status": code, "message": msg,
                "config_fields": rec.manifest.get("config"),
                "config": gateway.public_config(rec.id), "last_test": last_test}

    def _providers_payload(self):
        return {"providers": [self._provider_summary(rec) for rec in gateway.list_installed()],
                "active": gateway.active_all()}

    def _read_body_capped(self, cap):
        """The raw bytes a client posted, b"" for none, or None when it declared
        more than `cap` -- none of which was read off the socket, so the caller
        must 413 and hang up. _body() parses JSON on top of this; the provider
        upload calls it directly with a much larger cap, for the one POST body
        allowed to be a multi-megabyte tar.gz."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        if n > cap:
            return None
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _provider_upload_route(self, p):
        """POST /api/providers/install and .../<id>/update both accept
        EITHER a small {"url": ...} JSON body (install/update from a Stremio
        add-on manifest) OR a tar.gz package upload -- told apart by content
        (gzip magic bytes), not by Content-Type, since a client may not set
        it precisely. Handled outside do_POST's normal body cap, which is
        far too small for a real package.
        """
        if self._require_admin() is None:
            return
        raw = self._read_body_capped(admin.MAX_PACKAGE_BYTES)
        if raw is None:
            self.close_connection = True
            return self._send(413, {"err": "body too large"})
        is_gzip = raw[:2] == b"\x1f\x8b"
        payload = None
        if not is_gzip:
            try:
                parsed = json.loads(raw.decode("utf-8", "replace")) if raw else {}
                payload = parsed if isinstance(parsed, dict) else None
            except Exception:
                payload = None
            if payload is None:
                return self._send(400, {"err": "config",
                                        "message": "body is neither a JSON {url} object nor a tar.gz package"})
        if p.path == "/api/providers/install":
            return self._install_package(raw) if is_gzip else self._install_addon(payload)
        pid = p.path[len("/api/providers/"):-len("/update")]
        if not pid:
            return self._send(404, {"err": "not found"})
        return self._update_package(pid, raw) if is_gzip else self._update_addon(pid, payload)

    def _install_addon(self, payload):
        url = (payload or {}).get("url")
        if not url:
            return self._send(400, {"err": "no url"})
        try:
            raw = gateway.fetch_addon_manifest(url)
            manifest = gateway.addon_manifest_to_provider(raw, url)
            rec = gateway.install_provider(manifest, source=url)
        except contract.ProviderError as ex:
            return self._provider_error(ex)
        except contract.ContractError as ex:
            return self._send(400, {"err": "config", "code": ex.code,
                                    "message": contract.redact(ex.message, gateway.secret_values())})
        gateway.invalidate(rec.id)
        return self._send(200, self._provider_summary(rec))

    def _install_package(self, raw):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                admin._safe_extract_tar(raw, tmp)
                manifest = admin._read_package_manifest(tmp)
                rec = gateway.install_provider(manifest, files_dir=tmp, source="package")
            except ValueError as ex:
                return self._send(400, {"err": "config", "message": str(ex)})
            except contract.ContractError as ex:
                return self._send(400, {"err": "config", "code": ex.code, "message": ex.message})
        gateway.invalidate(rec.id)
        return self._send(200, self._provider_summary(rec))

    def _update_addon(self, pid, payload):
        url = (payload or {}).get("url")
        if not url:
            return self._send(400, {"err": "no url"})
        try:
            raw = gateway.fetch_addon_manifest(url)
            manifest = gateway.addon_manifest_to_provider(raw, url)
            rec = gateway.update_provider(pid, manifest, source=url)
        except contract.ProviderError as ex:
            return self._provider_error(ex)
        except contract.ContractError as ex:
            return self._send(400, {"err": "config", "code": ex.code,
                                    "message": contract.redact(ex.message, gateway.secret_values())})
        if rec is None:
            return self._send(404, {"err": "not found"})
        gateway.invalidate(pid)
        return self._send(200, self._provider_summary(rec))

    def _update_package(self, pid, raw):
        with tempfile.TemporaryDirectory() as tmp:
            try:
                admin._safe_extract_tar(raw, tmp)
                manifest = admin._read_package_manifest(tmp)
                rec = gateway.update_provider(pid, manifest, files_dir=tmp, source="package")
            except ValueError as ex:
                return self._send(400, {"err": "config", "message": str(ex)})
            except contract.ContractError as ex:
                return self._send(400, {"err": "config", "code": ex.code, "message": ex.message})
        if rec is None:
            return self._send(404, {"err": "not found"})
        gateway.invalidate(pid)
        return self._send(200, self._provider_summary(rec))

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        netprofile.note_request(p.path)
        if not self._host_ok():
            self.close_connection = True
            return self._send(403, {"ok": False, "msg": "unrecognised host"})
        try:
            for match, handle in self.GET_ROUTES:
                if match(p.path):
                    return handle(self, p)
        except Exception as ex:
            return self._send(500, {"err": contract.redact(
                "%s: %s" % (type(ex).__name__, ex), gateway.secret_values())})
        self._send(404, {"err": "not found"})

    def _get_index(self, p):
        body = open(os.path.join(config.HERE, "index.html"), "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)

    def _get_static(self, p):
        # The few files the page pulls in besides itself (the wordmark's
        # face). Long-lived cache: a phone on the sofa should fetch a
        # 97 KB font once, not on every reload of a no-store page. A
        # new file gets a new path rather than a new version of this one.
        fn, ctype = netprofile.STATIC[p.path]
        try:
            body = open(os.path.join(config.HERE, "static", fn), "rb").read()
        except OSError:
            return self._send(404, {"error": "no such file"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(body)

    def _get_setup_state(self, p):
        # Unauthenticated by design -- the settings page needs to know
        # whether to show a login form or a setup form before there is
        # any session to prove who is asking. No secret, no provider
        # config value and no bootstrap token belongs in this response.
        st = gateway.setup_required()
        return self._send(200, {"claimed": gateway.admin_claimed(),
                                "configured": bool(st.get("configured")),
                                "roles": st.get("roles") or {},
                                "message": st.get("message") or ""})

    def _get_providers(self, p):
        if self._require_admin() is None:
            return
        return self._send(200, self._providers_payload())

    def _get_genres(self, p):
        q = urllib.parse.parse_qs(p.query)
        kind = q.get("kind", ["movie"])[0]
        if kind not in ("movie", "tv"):
            kind = "movie"
        return self._send(200, {"genres": catalogue.get_genres(kind)})

    def _get_movies_progress(self, p):
        # Cheap and lock-free: what the matching /api/movies is doing.
        q = urllib.parse.parse_qs(p.query)
        kind = q.get("kind", ["movie"])[0]
        if kind not in ("movie", "tv"):
            kind = "movie"
        g = [x for x in (q.get("genres", [""])[0]).split(",") if x]
        ex = [x for x in (q.get("exclude", [""])[0]).split(",") if x]
        srt = (q.get("sort", ["top"])[0] or "top")
        bias = q.get("bias", [None])[0]
        if bias is not None:
            bias = bias.lower() not in ("0", "", "false", "no", "off")
        _, _, _, key = catalogue.view_key(g, srt, ex, kind, bias)
        return self._send(200, catalogue.view_progress(key))

    def _get_shelf(self, p):
        # Favourites, newest first, films and series mixed. Rendered
        # from the stored snapshots rather than the pool: a favourite
        # is kept precisely so it survives the catalogue forgetting it.
        return self._send(200, {"items": shelf.favourites()})

    def _get_channel(self, p):
        # A channel id contains ':' (provider:local), so it travels
        # as a query param, never as a path segment.
        q = urllib.parse.parse_qs(p.query)
        cid = (q.get("id", [""])[0] or "").strip()
        if not cid:
            return self._send(400, {"err": "no id"})
        try:
            ops = gateway.channel_ops()
            view = shelf.channel_view(cid)
            if view["followed"]:
                item = shelf.stored_item(cid)
                if not item or not item.get("title"):
                    # Followed but never actually got a snap (e.g. an
                    # interrupted follow) -- one live fetch to fill it
                    # in; channel_watch() takes over refreshing it daily.
                    ch = channels.channel_details_cached(cid)
                    shelf.set_snap(cid, ch)
                    item = channels._channel_item(ch, cid)
            else:
                item = channels._channel_item(channels.channel_details_cached(cid), cid)
        except contract.ProviderError as ex:
            return self._provider_error(ex)
        body = dict(item)
        body["channel_ops"] = sorted(ops)
        return self._send(200, body)

    def _get_channel_videos(self, p):
        # Blank values kept: "page=" (the first page's next="" followed)
        # means "the full list from the start", and a parser that drops
        # it serves page one again, so the list never grows past it.
        q = urllib.parse.parse_qs(p.query, keep_blank_values=True)
        cid = (q.get("id", [""])[0] or "").strip()
        if not cid:
            return self._send(400, {"err": "no id"})
        paging = "page" in q
        page = (q.get("page", [""])[0] or "").strip()
        try:
            ops = gateway.channel_ops()
            if paging:
                if contract.OP_CH_VIDEOS not in ops:
                    return self._send(200, {"videos": [], "next": None})
                r = gateway.channel_videos(cid, page)
                videos, nxt = r["videos"], r["next"]
            else:
                if shelf.channel_view(cid)["followed"]:
                    stored = shelf.stored_latest(cid)
                    fresh = stored["checked"] is not None and \
                        time.time() - stored["checked"] < config.CHANNEL_POLL_MIN * 60
                    if fresh:
                        videos = stored["videos"]
                    else:
                        r = gateway.channel_latest(cid)
                        videos = r["videos"]
                        shelf.set_latest(cid, videos)
                else:
                    videos = gateway.channel_latest(cid)["videos"]
                # "" (never None) when channels.videos is available:
                # the client reads that as "call channels.videos with
                # no token for the full list" -- channels.latest has
                # no paging of its own to hand back a real one.
                nxt = "" if contract.OP_CH_VIDEOS in ops else None
        except contract.ProviderError as ex:
            return self._provider_error(ex)
        return self._send(200, {"videos": shelf.decorate_videos(cid, videos), "next": nxt})

    def _get_movies(self, p):
        q = urllib.parse.parse_qs(p.query)
        kind = q.get("kind", ["movie"])[0]
        if kind == "channel":
            # Its own shape, not a catalogue page: followed channels
            # (new uploads first), then Popular -- only when this
            # install's channels provider can answer that op at all.
            if not gateway.available(contract.ROLE_CHANNELS):
                return self._send(200, {"movies": [], "popular": [], "more": False,
                                        "offset": 0, "limit": 0,
                                        "err": "channels are not set up",
                                        "pool": 0, "checked": 0, "channel_ops": []})
            ops = gateway.channel_ops()
            movies = shelf.followed_channels()
            popular = []
            if contract.OP_CH_POPULAR in ops:
                followed_ids = {m["id"] for m in movies}
                popular = [channels._channel_item(ch) for ch in channels.channel_popular(seeds=followed_ids)
                          if ch.get("id") not in followed_ids]
            return self._send(200, {"movies": movies, "popular": popular, "more": False,
                                    "offset": 0, "limit": len(movies), "err": None,
                                    "pool": len(movies), "checked": 0,
                                    "channel_ops": sorted(ops)})
        if kind not in ("movie", "tv"):
            kind = "movie"
        g = [x for x in (q.get("genres", [""])[0]).split(",") if x]
        # unclamped, a large/negative offset forces resolution of the
        # whole pool (or negative-slice weirdness) for an unauthenticated caller
        off = max(0, min(int(q.get("offset", ["0"])[0]), config.POOL_MAX))
        lim = max(1, min(int(q.get("limit", [str(config.PAGE)])[0]), 50))
        srt = (q.get("sort", ["top"])[0] or "top")
        ex  = [x for x in (q.get("exclude", [""])[0]).split(",") if x]
        # absent -> the server default; the TV app always says which
        bias = q.get("bias", [None])[0]
        if bias is not None:
            bias = bias.lower() not in ("0", "", "false", "no", "off")
        ms, more, cursor, pool, perr = catalogue.get_page(g, off, lim, srt, ex, kind, bias)
        # Decorated COPIES. `ms` is a slice of the pool's own served
        # list, which every later request for this view is served
        # from -- a shelf state written into those dicts would be
        # baked into the cache and go on being sent long after it
        # stopped being true.
        body = {"movies": [shelf.decorate(dict(m)) for m in ms], "err": perr,
                "genres_applied": g,
                "excluded": ex,
                "bias": config.BIAS if bias is None else bias,
                "sort": srt if srt in config.SORTS else "top",
                "offset": off, "limit": lim, "more": more,
                "checked": cursor, "pool": pool}
        if off == 0:
            # First page only: the client prepends these and drops any
            # later catalogue item with the same id, so sending them
            # again further down the wall would only duplicate work.
            # Paging arithmetic above is untouched by them.
            body["pinned"] = watching.shelf_pins(kind)
        return self._send(200, body)

    def _get_search_stream(self, p):
        # Same search, pushed result-by-result. A cold franchise search
        # resolves ~60 candidates through the streams provider and takes
        # ~25s, but the first playable film is ready in about one -- so the page
        # should not sit empty waiting for the last one.
        sq = urllib.parse.parse_qs(p.query)
        term = (sq.get("q", [""])[0] or "").strip()
        if not term:
            return self._send(400, {"err": "no query"})
        lim = max(1, min(int(sq.get("limit", ["24"])[0]), 60))
        kind = sq.get("kind", ["movie"])[0]
        if kind == "channel":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            def emit(ev, obj):
                self.wfile.write(("event: %s\ndata: %s\n\n"
                                  % (ev, json.dumps(obj))).encode("utf-8"))
                self.wfile.flush()
            try:
                ops = gateway.channel_ops()
                # A link or an @handle names exactly one channel --
                # resolve it rather than search it, and fall back to
                # resolve for anything else when this install's
                # provider has no search index of its own.
                if "/" in term or term.startswith("@") or contract.OP_CH_SEARCH not in ops:
                    n = 1
                    emit("movie", channels._channel_item(gateway.channel_resolve(term)))
                else:
                    items = gateway.channel_search(term, lim)["items"]
                    n = len(items)
                    for ch in items:
                        emit("movie", channels._channel_item(ch))
                emit("done", {"playable": n, "found": n, "checked": n})
            except contract.ProviderError as ex:
                try:
                    emit("fail", {"err": ex.message})
                except Exception:
                    pass
            except (BrokenPipeError, ConnectionResetError):
                pass              # searched again, or closed the popup
            except Exception as ex:
                try:
                    emit("fail", {"err": "%s: %s" % (type(ex).__name__, ex)})
                except Exception:
                    pass
            return
        if kind not in ("movie", "tv"):
            kind = "movie"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        def emit(ev, obj):
            self.wfile.write(("event: %s\ndata: %s\n\n"
                              % (ev, json.dumps(obj))).encode("utf-8"))
            self.wfile.flush()
        try:
            ms, found, checked = catalogue.search_movies(
                term, lim,
                on_found=lambda c: emit("found", {"found": c}),
                on_movie=lambda m: emit("movie", shelf.decorate(dict(m))),
                kind=kind)
            emit("done", {"playable": len(ms), "found": found, "checked": checked})
        except (BrokenPipeError, ConnectionResetError):
            pass                  # searched again, or closed the popup
        except Exception as ex:
            try:
                emit("fail", {"err": "%s: %s" % (type(ex).__name__, ex)})
            except Exception:
                pass

    def _get_search(self, p):
        q = urllib.parse.parse_qs(p.query)
        term = (q.get("q", [""])[0] or "").strip()
        if not term:
            return self._send(400, {"err": "no query"})
        lim = max(1, min(int(q.get("limit", ["24"])[0]), 60))
        kind = q.get("kind", ["movie"])[0]
        if kind not in ("movie", "tv"):
            kind = "movie"
        ms, found, checked = catalogue.search_movies(term, lim, kind=kind)
        return self._send(200, {"q": term,
                                "movies": [shelf.decorate(dict(m)) for m in ms],
                                "playable": len(ms),
                                "found": found, "checked": checked})

    def _get_movie(self, p):
        tid = self._id(p.path.rsplit("/", 1)[-1])
        d = gateway.details(tid, contract.KIND_MOVIE)
        # The catalogue's own rating, as it always was here; the IMDb
        # one is the tile's badge (imdb.rating), not this.
        tr = streams.own_rating(d)
        return self._send(200, shelf.decorate({
            "id": d.get("id"), "title": d.get("title"),
            "tagline": d.get("tagline"), "overview": d.get("overview"),
            "runtime": d.get("runtime"), "vote": tr[0] if tr else None,
            "votes": tr[1] if tr else None, "release": d.get("release_date"),
            # `year` as well as `release`: a provider may know a title's
            # year without knowing its exact release date, and the grid
            # shows the year. Deriving it from `release` on the client
            # loses exactly those titles.
            "year": d.get("year"),
            "genres": d.get("genres") or [],
            "backdrop": d.get("backdrop"), "poster": d.get("poster"),
            "imdb_id": (d.get("external_ids") or {}).get("imdb")}))

    def _get_tv_season(self, p):
        bits = p.path[len("/api/tv/"):].split("/")
        bits[0] = self._id(bits[0])
        if len(bits) != 3 or bits[1] != "season":
            return self._send(404, {"err": "not found"})
        tid, n = bits[0], int(bits[2])
        e = catalogue.tv_season(tid, n)
        # Copies again: this dict IS the 24h season cache, and a
        # watched flag written into it would outlive the fact.
        return self._send(200, dict(e, episodes=[
            dict(ep, **watching._ep_view(tid, n, ep)) for ep in e["episodes"]]))

    def _get_tv(self, p):
        tid = self._id(p.path.rsplit("/", 1)[-1])
        return self._send(200, shelf.decorate(dict(catalogue.tv_detail(tid))))

    def _get_stream_tv(self, p):
        bits = p.path[len("/api/stream/tv/"):].split("/")
        bits[0] = self._id(bits[0])
        if len(bits) != 3:
            return self._send(404, {"err": "not found"})
        tid, s, ep = bits
        force = "force" in urllib.parse.parse_qs(p.query)
        e = streams.get_stream_tv(tid, int(s), int(ep), force=force)
        pick = e["pick"]
        # rejected: so a client showing "N streams, 0 playable" can
        # also say why -- codec, hardsub, language, budget...
        return self._send(200, {"pick": pick, "count": e["count"], "err": e["err"],
                                "rejected": e.get("rejected") or {},
                                "url": streams.stream_url(pick) if pick else None})

    def _get_stream(self, p):
        tid = self._id(p.path.rsplit("/", 1)[-1])
        force = "force" in urllib.parse.parse_qs(p.query)
        e = streams.get_stream(tid, force=force)
        pick = e["pick"]
        return self._send(200, {"pick": pick, "count": e["count"], "err": e["err"],
                                "rejected": e.get("rejected") or {},
                                "url": streams.stream_url(pick) if pick else None})

    def _get_progress(self, p):
        return self._send(200, jobs.job_get(self._id(p.path.rsplit("/", 1)[-1])))

    def _get_bx_probes(self, p):
        # Unauthenticated and cheap: a static list, published so the
        # page and this server cannot drift apart on which codec
        # strings caps["types"] is keyed by -- both sides build
        # against browser_play.CODEC_PROBES, but only this process
        # can prove which version of it actually shipped.
        # "audio" is the subset of "probes" that names an audio
        # codec. Every probe is published as video/mp4, so the page
        # cannot work that out for itself -- and without it the
        # pairing probes it builds have nothing to pair.
        return self._send(200, {"probes": list(browser_play.CODEC_PROBES),
                                "audio": list(browser_play.AUDIO_PROBES)})

    def _get_src(self, p):
        return self._proxy_source(p.path[len("/src/"):].strip("/"))

    def _get_torrent(self, p):
        bits = [x for x in p.path[len("/t/"):].split("/") if x]
        idx = bits[1] if len(bits) > 1 and bits[1].isdigit() else None
        return self._proxy_torrent(bits[0] if bits else "", idx)

    def _get_audio(self, p):
        # Serve the converted file from disk, following it as ffmpeg
        # appends. A real file means a player reconnect resumes from a
        # byte offset instead of restarting the film from zero.
        bits = [x for x in p.path[len("/audio/"):].split("/") if x]
        if not bits or not contract.RE_HASH40.match(bits[0]):
            return self._send(400, {"err": "bad infoHash"})
        ih = bits[0]
        idx = bits[1] if len(bits) > 1 and bits[1].isdigit() else None
        name = transcode.tc_name(ih, idx)
        path = os.path.join(config.TC_HOST, name)
        if not os.path.exists(path):
            return self._send(404, {"err": "no converted stream for this title"})
        start_at = 0
        rng = self.headers.get("Range") or ""
        m = re.match(r"bytes=(\d+)-", rng)
        if m:
            start_at = int(m.group(1))
        # Logged because "missing the opening sequence" would be explained
        # by the player seeking to the live edge of a growing file rather
        # than starting at byte 0.
        try:
            cur = os.path.getsize(path)
        except OSError:
            cur = 0
        print("audio: %s range=%r -> start=%d of %d bytes on disk"
              % (name, rng or "none", start_at, cur), flush=True)
        # A finished conversion is just a file, and must be served like
        # one. Without a length the player cannot tell "the film ended"
        # from "the connection dropped", so on reaching the last byte it
        # reconnected at offset 0 and played the whole film again -- the
        # looping. A real length also makes the file properly seekable.
        done = not transcode.transcode_writing(ih, name)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Accept-Ranges", "bytes")
        if done:
            end = max(start_at, cur - 1)
            if m:
                self.send_header("Content-Range",
                                 "bytes %d-%d/%d" % (start_at, end, cur))
            self.send_header("Content-Length", str(max(0, cur - start_at)))
        elif m:
            # Still converting, so the total genuinely is unknown: RFC
            # 7233 allows "*" there, but the end must still be a number.
            self.send_header("Content-Range",
                             "bytes %d-%d/*" % (start_at, max(start_at, cur - 1)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        # Clock time, not a count of 0.1 s naps: transcode_writing()
        # can take seconds when it has to ask the container, and a
        # count of naps ran long by however much each pass cost.
        stalled_at = None     # when the file last stopped growing
        held_at = None        # when the regulator last started holding it
        try:
            with open(path, "rb") as f:
                f.seek(start_at)
                while True:
                    chunk = f.read(262144)
                    if chunk:
                        stalled_at = held_at = None
                        self.wfile.write(chunk)
                        continue
                    if not transcode.transcode_writing(ih, name):
                        break            # conversion finished, file complete
                    now = time.monotonic()
                    if transcode.transcode_suspended(ih):
                        # The regulator is holding it back on purpose --
                        # seek forward and the player reads to the live
                        # edge, where 60s of "no growth" used to look
                        # like EOF and hang up mid-film.
                        stalled_at = None
                        held_at = held_at or now
                        if now - held_at > 1200:
                            break        # 20 min suspended: regulator gone
                        time.sleep(0.1)
                        continue
                    held_at = None
                    stalled_at = stalled_at or now
                    if now - stalled_at > 60:
                        break            # 60s with no new data: give up
                    time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass                          # player went away; expected

    def _get_hls(self, p):
        # On-demand browser HLS: index.m3u8, init.mp4, and the
        # per-segment fMP4 files, all under /hls/<token>/... The
        # token IS the route: checking it against _bx["token"] here
        # is the whole mechanism that keeps a stale browser tab's
        # requests from ever reaching a session that replaced it --
        # there is no other check anywhere downstream of this one.
        bits = [x for x in p.path[len("/hls/"):].split("/") if x]
        if len(bits) != 2 or not browser_play.RE_TOKEN.match(bits[0]):
            return self._send(404, {"err": "not found"})
        token, leaf = bits
        with core._lock:
            if browser_session._bx["token"] != token:
                return self._send(404, {"err": "not found"})
            sess_dir, seg, dur = browser_session._bx["dir"], browser_session._bx["seg"], browser_session._bx["dur"]
            n_segs = browser_session._bx["n_segs"]

        if leaf == "index.m3u8":
            body = browser_play.vod_playlist(dur, seg)
            if body is None:
                return self._send(404, {"err": "not found"})
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        if leaf == "init.mp4":
            init_path = os.path.join(sess_dir, "init.mp4")
            t0 = time.time()
            while not os.path.exists(init_path):
                with core._lock:
                    if browser_session._bx["token"] != token:
                        return self._send(404, {"err": "not found"})
                if time.time() - t0 > config.BX_SEG_WAIT:
                    return self._retry()
                time.sleep(0.1)
            try:
                with open(init_path, "rb") as f:
                    data = f.read()
            except OSError:
                return self._retry()
            self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        m = re.match(r"^s(\d{6})\.m4s$", leaf)
        if not m:
            return self._send(404, {"err": "not found"})
        k = int(m.group(1))
        if n_segs is None or k >= n_segs:
            return self._send(404, {"err": "not found"})

        key = "bx:" + token
        seg_path = os.path.join(sess_dir, "s%06d.m4s" % k)
        succ_path = os.path.join(sess_dir, "s%06d.m4s" % (k + 1))
        # The last slot in the playlist has to carry the end of the
        # film, and a run that seeked does not fit the grid it was
        # numbered against.
        #
        # BUG a seek makes ffmpeg start at the keyframe at or before
        # k0*seg -- EARLIER than the grid point, by design (see
        # bx_spawn) -- so that run has more film left to write than
        # the grid budgeted slots for, and it numbers the overflow
        # past the end of the playlist. Measured: seeking to 40s in
        # a 60s clip on a 4s grid wrote s000010..s000015, six files
        # for the five slots 10..14 that the playlist has. s000015
        # held the last 2.04s, the playlist never named it, and the
        # film ended 2s early -- silently, because nothing errored:
        # the player simply ran out of playlist.
        #
        # So the final slot serves its own file AND every file after
        # it. They are consecutive fragments of one continuous run,
        # already stamped on one timeline by the shift below, so
        # what the player gets is the real end of the film.
        last_slot = (k == n_segs - 1)

        def _bx_complete():
            # The next file existing is what proves the muxer closed
            # this one -- it only finalizes segment k the instant it
            # opens k+1. The one exception is the process being gone
            # entirely: nothing will ever open a successor then, so
            # whatever is on disk for k is all there is ever going to
            # be, and has to be served as final rather than waited on
            # forever.
            if last_slot:
                # A successor file proves nothing here: on the final
                # slot the successor is part of THIS response, so it
                # has to be finished too. Only the run being over
                # settles that -- and a run that has reached the last
                # slot has reached the end of the film, so it is
                # about to end anyway.
                return os.path.exists(seg_path) and not transcode.transcode_alive(key)
            return os.path.exists(seg_path) and (
                os.path.exists(succ_path) or not transcode.transcode_alive(key))

        if not _bx_complete():
            frontier = browser_session.bx_frontier(token)
            if frontier is None:
                return self._send(404, {"err": "not found"})
            near = frontier <= k <= frontier + config.BX_LOOKAHEAD
            if not near:
                # Far from the frontier: a seek. There is no separate
                # seek API for an HLS <video> element to call, so the
                # segment request IS the seek channel -- this is how
                # the player tells the server it moved. Debounce it
                # so a drag across many segments collapses into one
                # restart instead of one per segment the scrub
                # passes over; seek_gen is what lets a later request
                # (a newer point in the same drag) cancel this one.
                with core._lock:
                    if browser_session._bx["token"] != token:
                        return self._send(404, {"err": "not found"})
                    browser_session._bx["pending_anchor"] = k
                    browser_session._bx["seek_gen"] += 1
                time.sleep(config.BX_SEEK_DEBOUNCE)
                with core._lock:
                    still = (browser_session._bx["token"] == token
                            and browser_session._bx["pending_anchor"] == k)
                if still:
                    browser_session.bx_restart(token, k)
                # either way, fall through to the long-poll below

            t0 = time.time()
            while not _bx_complete():
                with core._lock:
                    gen = browser_session._bx["gen"]
                    alive = browser_session._bx["token"] == token
                if not alive or jobs.superseded(gen):
                    return self._send(404, {"err": "not found"})
                if time.time() - t0 > config.BX_SEG_WAIT:
                    return self._retry()
                time.sleep(0.1)

        try:
            with open(seg_path, "rb") as f:
                data = f.read()
            if last_slot:
                # Re-listed here rather than reused from above: the
                # long poll may have waited a while, and the run may
                # have written more of the tail in the meantime.
                j = k + 1
                while True:
                    more = os.path.join(sess_dir, "s%06d.m4s" % j)
                    if not os.path.exists(more):
                        break
                    with open(more, "rb") as f:
                        # Only the fragments: appending whole files
                        # would leave a styp and two sidx boxes in
                        # the middle of a media segment.
                        data += browser_play.fragments_only(f.read())
                    j += 1
        except OSError:
            return self._retry()

        timescales = browser_session.bx_timescales(token)
        if not timescales:
            return self._retry()
        with core._lock:
            if browser_session._bx["token"] != token:
                return self._send(404, {"err": "not found"})
            run_anchor = browser_session._bx["run_anchor"]

        # What is added is the RUN's real start time, once, to every
        # segment that run wrote -- not this segment's own grid
        # position.
        #
        # BUG it used to add seg_start(k, seg), reasoning that k*seg
        # is segment k's absolute position on the grid. That part is
        # true; adding it was not. -ss is an input seek and -copyts
        # is never passed, so ffmpeg zeroes the run's clock at the
        # keyframe it seeked to and then counts up CONTINUOUSLY
        # across every segment that run writes -- the second segment
        # of a run already carries a segment's worth of ticks in its
        # own bytes. Adding k*seg on top counted the same elapsed
        # time twice: on the unseeked run, the segment holding 6s of
        # the film was served stamped 12s, and the error grew with
        # k. The bytes are missing exactly one thing, the offset
        # ffmpeg threw away when it zeroed its clock, and that is
        # the run's anchor.
        #
        # Adding the anchor rather than the grid position is also
        # what keeps consecutive segments gapless: ffmpeg's own
        # count is contiguous within a run, so shifting the whole
        # run by one constant preserves that, while stamping each
        # segment at k*seg would have forced a gap or an overlap
        # wherever a keyframe made the real segment longer or
        # shorter than the grid promised.
        #
        # This needs every file present to belong to the current
        # run, which bx_spawn guarantees by clearing the older ones
        # before the run starts.
        deltas = browser_play.deltas_for(timescales, run_anchor)
        try:
            data, _trafs, _sidx = browser_play.shift_timeline(data, deltas)
        except browser_play.TfdtPatchError as ex:
            # A silently mis-stamped segment plays back with a
            # timestamp that looks plausible and is not -- far worse
            # than a failed request, so this is a hard stop, logged
            # for whoever has to work out which segment went wrong.
            print("bx: tfdt patch failed for %s seg %06d: %s"
                  % (token[:8], k, contract.redact(str(ex), gateway.secret_values())),
                  flush=True)
            return self._send(500, {"err": "segment could not be timestamped"})

        total = len(data)
        start, end, status = 0, total - 1, 200
        rng = self.headers.get("Range") or ""
        mrange = re.match(r"bytes=(\d+)-(\d*)", rng)
        if mrange:
            start = int(mrange.group(1))
            end = int(mrange.group(2)) if mrange.group(2) else total - 1
            end = min(end, total - 1)
            if start > end or start >= total:
                return self._send(416, {"err": "range not satisfiable"})
            status = 206
        chunk = data[start:end + 1]
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.send_header("Content-Length", str(len(chunk)))
        # Unlike /audio/, deliberately keep-alive: segments are many
        # and small, and a fresh TCP+TLS-less handshake per one would
        # cost more than the segment itself on a slow link.
        self.end_headers()
        self.wfile.write(chunk)

    def _get_nowplaying(self, p):
        app = tvlink.app_fresh()
        st = tvlink.tv_playback_state()
        live = st in (2, 3)
        known = live and bool(nowplaying._now.get("title"))
        # _now records what was last HANDED OVER, which is empty after a
        # restart and wrong if the app was told to play by something else.
        # The app knows what it actually has open, so it wins on the title.
        title = nowplaying._now.get("title") if known else None
        if app and live and not title and app.get("title"):
            title = str(app["title"])[:200]
        return self._send(200, {
            "playing": st == 3,
            "paused": st == 2,
            "live": live,
            "state": st,
            # who the state above came from, so the phone knows whether
            # a position is worth showing
            "source": "app" if app else
                      ("adb" if (config.ADB_ENABLED and st is not None) else None),
            "position_s": app.get("position_s") if app else None,
            "duration_s": app.get("duration_s") if app else None,
            "app": {"name": app["name"], "version": app["version"],
                    "seen_s": round(time.time() - app["seen_at"], 1)}
                   if app else None,
            "title": title,
            "tag": nowplaying._now.get("tag") if known else None,
            "audio": nowplaying._now.get("audio") if known else None,
            "transcoded": nowplaying._now.get("transcoded") if known else None,
            # The size that ACTUALLY played. run_play_job walks up to
            # ATTEMPTS candidates and quietly falls through when one is
            # too slow, so the film on screen can be a different release
            # from the one whose size the grid advertised. Without this
            # the interface could never tell you that happened.
            "gb": nowplaying._now.get("gb") if known else None,
            "kind": nowplaying._now.get("kind"),
            "season": nowplaying._now.get("season"),
            "episode": nowplaying._now.get("episode"),
            "next": nowplaying._now.get("next"),
            "hifi": {"on": sendspin._hifi["on"], "gen": sendspin._hifi["gen"],
                     "err_s": sendspin._hifi["err_s"], "streaming": sendspin._hifi["streaming"],
                     "pending": sendspin._hifi["pending_since"] > 0,
                     "delay_ms": sendspin._hifi["delay_ms"],
                     "applied_delay_ms": sendspin._hifi["applied_delay_ms"],
                     "connected": sendspin._hifi["connected"],
                     "player_url": sendspin._hifi["player_url"],
                     "last_error": sendspin._hifi["last_error"],
                     "fail_count": sendspin._hifi["fail_count"],
                     "supply": sendspin._hifi["supply"],
                     "cache": sendspin._hifi["cache"]},
        })

    def _get_hifi_players(self, p):
        # Request-handler thread, not the heartbeat path -- calling
        # the bridge directly here is fine (see _ss_call's own doc).
        status, body = sendspin._ss_call("/players", timeout=3.0)
        if status != 200:
            return self._send(200, {"ok": False, "players": [],
                                    "error": body.get("error") or "bridge unreachable"})
        return self._send(200, {"ok": True, **body})

    def _get_netcheck(self, p):
        # The page polls this every 2s while calibrating, so it must
        # never block behind a writer. A status read is not worth
        # waiting on: report progress and move on.
        if not core._lock.acquire(timeout=2):
            return self._send(200, {"busy": netprofile._net_busy["on"],
                                    "calibrating": netprofile._cal["on"],
                                    "cal_done": netprofile._cal["done"],
                                    "cal_want": netprofile._cal["want"],
                                    "cal_msg": netprofile._cal["msg"] or "working\u2026",
                                    "stale_read": True})
        try:
            prof = dict(netprofile._net)
        finally:
            core._lock.release()
        # What a film is actually held to, the speed lid included --
        # the measured figure stays in "mbps".
        prof["sustain_live"] = netprofile.sustainable_mbps(None)
        prof.update(netprofile.limits())
        # first run has no profile at all, so the page can say what the
        # wait is for rather than just looking slow
        prof["busy"] = netprofile._net_busy["on"]
        prof["first_run"] = not prof.get("at")
        # recount both: record_rate() appends from real playback, so the
        # figure frozen at the last derive drifts behind the list itself
        prof["n_samples"] = len(prof.get("samples") or [])
        prof["n_ordinary"] = len(netprofile.ordinary_samples())
        prof["needs"] = max(0, config.NET_MIN_OBS - prof["n_ordinary"])
        prof["well_seeded_at"] = config.WELL_SEEDED
        prof["calibrating"] = netprofile._cal["on"]
        prof["cal_done"] = netprofile._cal["done"]
        prof["cal_want"] = netprofile._cal["want"]
        prof["cal_msg"] = netprofile._cal["msg"]
        prof["age_h"] = round((time.time() - float(prof.get("at") or 0)) / 3600, 1) \
                        if prof.get("at") else None
        return self._send(200, prof)

    def _get_health(self, p):
        # The phone polls this every 15s, so it must never have side
        # effects -- adb_ready() can run `adb connect` and sleep for
        # several seconds, which turned a powered-off TV into constant
        # adb churn and a multi-second response. /api/reconnect is the
        # endpoint that actually reconnects.
        app = tvlink.app_fresh()
        st = tvlink.adb_state()          # "off", and no adb run at all, when disabled
        if app:
            msg = "TV app connected (%s)" % (app["name"] or app["id"] or "unnamed")
        elif config.ADB_ENABLED:
            msg = {"device": "connected",
                   "unauthorized": "TV is waiting for you to accept the USB-debugging prompt on screen",
                   }.get(st, f"adb state: {st}")
        else:
            # Nothing is connected and there is no fallback to offer, so
            # say the thing the user can act on rather than an adb state
            # they have deliberately turned off.
            msg = "no TV app connected"
        return self._send(200, {"tv": bool(app) or (config.ADB_ENABLED and st == "device"),
                                "tv_msg": msg, "tv_state": st,
                                "app": {"id": app["id"], "name": app["name"],
                                        "version": app["version"],
                                        "state": app["state"],
                                        "seen_s": round(time.time() - app["seen_at"], 1)}
                                       if app else None,
                                "adb": {"enabled": config.ADB_ENABLED, "state": st},
                                "movies": catalogue.pool_served(),
                                "page_size": config.PAGE,
                                "streams_cached": len(streams._streams),
                                "providers": admin.providers_health(),
                                "fourk_only": config.FOURK_ONLY, "hevc_only": config.HEVC_ONLY,
                                "autoplay": config.AUTOPLAY_NEXT,
                                "min_seeders": config.MIN_SEEDERS,
                                "max_gb": netprofile.max_gb(),
                                "sustain_mbps": netprofile.sustainable_mbps(None),
                                "conns": netprofile._net.get("conns"),
                                "net_source": netprofile._net.get("source")})

    def do_POST(self):
        p = urllib.parse.urlparse(self.path)
        netprofile.note_request(p.path)
        # Addressed to a name that is not ours: rebinding, and the body is
        # still unread, so the connection goes with it.
        if not self._host_ok():
            self.close_connection = True
            return self._send(403, {"ok": False, "msg": "unrecognised host"})
        # Every mutating route lives behind this: reject a forged cross-site
        # POST before it touches anything.
        if not self._origin_ok():
            # The body is still sitting unread on the socket -- this
            # connection cannot be reused for anything, so close it rather
            # than parse the next request out of the middle of it.
            self.close_connection = True
            return self._send(403, {"ok": False, "msg": "cross-site request rejected"})
        # Provider install/update accept a tar.gz package upload, which the
        # small JSON cap below cannot carry -- handled first, and entirely
        # separately, so a real package never hits the 413 meant for a
        # misbehaving JSON client.
        if p.path == "/api/providers/install" or (
                p.path.startswith("/api/providers/") and p.path.endswith("/update")):
            try:
                return self._provider_upload_route(p)
            except Exception as ex:
                return self._send(500, {"err": contract.redact(
                    "%s: %s" % (type(ex).__name__, ex), gateway.secret_values())})
        # Every POST route needs its body off the socket before it can be
        # routed at all: on a reused keep-alive connection, leaving it
        # unread means the next request is parsed out of the middle of it.
        d = self._body()
        if d is None:
            # Oversize body, and none of it has been read. This connection
            # cannot be reused for anything, so close it rather than parse
            # the next request out of the middle of a body the app is still
            # sending.
            self.close_connection = True
            return self._send(413, {"ok": False, "msg": "body too large"})
        try:
            for match, handle in self.POST_ROUTES:
                if match(p.path):
                    return handle(self, p, d)
        except Exception as ex:
            return self._send(500, {"ok": False, "msg": contract.redact(
                "%s: %s" % (type(ex).__name__, ex), gateway.secret_values())})
        self._send(404, {"err": "not found"})

    def _post_setup_claim(self, p, d):
        # Single-use: store.claim() itself refuses once a password
        # already exists, regardless of what token is presented.
        token, password = d.get("token"), d.get("password")
        if not token or not password or len(str(password)) < 8:
            return self._send(400, {"ok": False, "err":
                                    "token and an 8+ character password are required"})
        if not gateway.claim_setup(str(token), str(password)):
            return self._send(400, {"ok": False, "err": "invalid or already-used setup token"})
        return self._send(200, {"ok": True})

    def _post_admin_login(self, p, d):
        client = self.client_address[0]
        wait = gateway.login_wait(client)
        if wait:
            return self._send(429, {"ok": False, "err": "too many attempts",
                                    "retry_after": wait})
        password = d.get("password")
        ok = bool(password) and gateway.check_admin_password(str(password))
        gateway.note_login(client, ok)
        if not ok:
            return self._send(401, {"ok": False, "err": "admin required"})
        tok = gateway.new_session()
        csrf = gateway.csrf_for(tok)
        body = json.dumps({"ok": True, "csrf": csrf}).encode()
        self.send_response(200)
        self.send_header("Set-Cookie", "%s=%s; HttpOnly; SameSite=Strict; Path=/" % (admin.ADMIN_COOKIE, tok))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _post_admin_logout(self, p, d):
        tok = self._require_admin()
        if tok is None:
            return
        gateway.drop_session(tok)
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Set-Cookie", "%s=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0" % admin.ADMIN_COOKIE)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _post_providers_preview(self, p, d):
        # See-before-you-install: convert and validate a manifest URL
        # without installing anything.
        if self._require_admin() is None:
            return
        url = d.get("url")
        if not url:
            return self._send(400, {"err": "no url"})
        try:
            raw = gateway.fetch_addon_manifest(url)
            manifest = gateway.addon_manifest_to_provider(raw, url)
        except contract.ProviderError as ex:
            return self._provider_error(ex)
        except contract.ContractError as ex:
            return self._send(400, {"err": "config", "code": ex.code,
                                    "message": contract.redact(ex.message, gateway.secret_values())})
        return self._send(200, {"id": manifest["id"], "name": manifest["name"],
                                "version": manifest["version"],
                                "capabilities": manifest["capabilities"],
                                "config": manifest["config"]})

    def _post_providers_active(self, p, d):
        if self._require_admin() is None:
            return
        role, pid = d.get("role"), d.get("provider_id")
        if role not in contract.ROLES:
            return self._send(400, {"err": "unknown role"})
        try:
            gateway.set_active(role, pid)
        except ValueError as ex:
            return self._send(400, {"err": str(ex)})
        gateway.invalidate(pid if pid else None)
        return self._send(200, {"ok": True, "active": gateway.active_all()})

    def _post_provider_config(self, p, d):
        pid = p.path[len("/api/providers/"):-len("/config")]
        if self._require_admin() is None:
            return
        if gateway.get_provider(pid) is None:
            return self._send(404, {"err": "not found"})
        values, clear = d.get("values") or {}, d.get("clear") or []
        if not isinstance(values, dict) or not isinstance(clear, list):
            return self._send(400, {"err": "values must be an object, clear a list"})
        gateway.set_config(pid, values, clear_keys=[str(k) for k in clear])
        gateway.invalidate(pid)
        return self._send(200, {"ok": True, "config": gateway.public_config(pid)})

    def _post_provider_test(self, p, d):
        pid = p.path[len("/api/providers/"):-len("/test")]
        if self._require_admin() is None:
            return
        if gateway.get_provider(pid) is None:
            return self._send(404, {"err": "not found"})
        # A failed test is a normal, expected outcome here -- captured
        # into the stored/returned result rather than surfaced as an
        # HTTP error, so the admin UI can show it inline.
        try:
            result = gateway.test(pid)
            record = {"ok": True, "at": time.time(), "result": result}
        except contract.ProviderError as ex:
            record = {"ok": False, "at": time.time(), "code": ex.code,
                     "message": contract.redact(ex.message, gateway.secret_values())}
        gateway.record_test(pid, record)
        return self._send(200, record)

    def _post_provider_enable(self, p, d):
        pid = p.path[len("/api/providers/"):-len("/enable")]
        if self._require_admin() is None:
            return
        if gateway.get_provider(pid) is None:
            return self._send(404, {"err": "not found"})
        enabled = bool(d.get("enabled"))
        gateway.set_enabled(pid, enabled)
        gateway.invalidate(pid)
        return self._send(200, {"ok": True, "enabled": enabled})

    def _post_provider_remove(self, p, d):
        pid = p.path[len("/api/providers/"):-len("/remove")]
        if self._require_admin() is None:
            return
        ok = gateway.remove_provider(pid)
        gateway.invalidate(pid)
        return self._send(200, {"ok": ok})

    def _post_heartbeat(self, p, d):
        # The TV app's whole connection to the server: its state in, the
        # next command out. Can block for up to the `wait` it asks for,
        # which is the point -- see app_heartbeat().
        return self._send(200, tvlink.app_heartbeat(d))

    def _post_hifi_delay(self, p, d):
        # Live lip-sync trim, for dialling it in from a laptop while
        # watching; the TV's own Settings value takes over on its
        # next heartbeat if it carries one.
        try:
            with core._lock:
                ms = sendspin._hifi_set_delay(d.get("ms", 0), persist=True)
        except (TypeError, ValueError):
            return self._send(400, {"ok": False, "msg": "ms must be an integer"})
        return self._send(200, {"ok": True, "delay_ms": ms})

    def _post_volume(self, p, d):
        # Enqueue only -- the bridge call happens on _ss_worker's
        # thread, same as every other sendspin action.
        payload = {"delta": d["delta"]} if "delta" in d else {"level": d.get("level")}
        sendspin._ss_q.put(("volume", payload))
        return self._send(200, {"ok": True})

    # The three shelf writes. Household actions, exactly like a play
    # or a stop: behind the host and origin guards every POST here is
    # behind, and not behind _require_admin -- marking a film watched
    # is not administering the install. Each forces a save: these are
    # deliberate, one-at-a-time acts, and a restart losing the last
    # one would be plainly wrong in a way a dropped heartbeat is not.
    def _post_shelf_fav(self, p, d):
        tid = str(d.get("id") or "").strip()
        if not tid:
            return self._send(400, {"ok": False, "msg": "id is required"})
        snap = d.get("snap") if isinstance(d.get("snap"), dict) else None
        shelf.set_fav(tid, bool(d.get("on")), snap=snap)
        shelf.save(force=True)
        return self._send(200, {"ok": True, "shelf": shelf.view(tid)})

    def _post_shelf_watched(self, p, d):
        tid = str(d.get("id") or "").strip()
        if not tid:
            return self._send(400, {"ok": False, "msg": "id is required"})
        # s and e together mean one episode; either missing means the
        # whole title, so a half-given pair is a mistake worth saying
        # rather than silently marking a whole series watched.
        s, e = d.get("s"), d.get("e")
        if s is not None or e is not None:
            try:
                s, e = int(s), int(e)
            except (TypeError, ValueError):
                return self._send(400, {"ok": False,
                                        "msg": "s and e must both be numbers"})
        shelf.set_watched(tid, bool(d.get("on")), s=s, e=e)
        shelf.save(force=True)
        return self._send(200, {"ok": True, "shelf": shelf.view(tid)})

    def _post_shelf_drop(self, p, d):
        # "Done with this". The job form is what a player sends as it
        # stops: it mutes that exact job, so the stop arriving right
        # behind it cannot record the position back again.
        jb = str(d.get("job") or "").strip() or None
        tid = str(d.get("id") or "").strip() or None
        if not jb and not tid:
            return self._send(400, {"ok": False, "msg": "id or job is required"})
        shelf.drop(title_id=tid, job=jb)
        shelf.save(force=True)
        return self._send(200, {"ok": True})

    # The channel writes. Same household-action footing as the shelf
    # writes just above: no admin gate, one save per act.
    def _post_channel_follow(self, p, d):
        cid = str(d.get("id") or "").strip()
        if not cid:
            return self._send(400, {"ok": False, "msg": "id is required"})
        on = bool(d.get("on"))
        if on:
            try:
                ch = channels.channel_details_cached(cid)
            except contract.ProviderError as ex:
                return self._provider_error(ex)
            shelf.follow(cid, True, snap=ch)
            shelf.save(force=True)
            def _prime():
                # Fills the tile's upload count right away, rather
                # than leaving it at 0 until channel_watch()'s next
                # sweep (up to CHANNEL_POLL_MIN minutes away).
                try:
                    r = gateway.channel_latest(cid)
                    shelf.set_latest(cid, r["videos"])
                    shelf.save(force=True)
                except contract.ProviderError:
                    pass
            threading.Thread(target=_prime, daemon=True).start()
            item = channels._channel_item(ch, cid)
        else:
            shelf.follow(cid, False)
            shelf.save(force=True)
            item = shelf.stored_item(cid) or {"id": cid, "kind": "channel",
                                               "followed": False, "new": 0}
        return self._send(200, {"ok": True, "channel": item})

    def _post_channel_seen(self, p, d):
        cid = str(d.get("id") or "").strip()
        if not cid:
            return self._send(400, {"ok": False, "msg": "id is required"})
        shelf.channel_seen(cid)
        shelf.save(force=True)
        return self._send(200, {"ok": True})

    def _post_channel_opened(self, p, d):
        cid = str(d.get("id") or "").strip()
        vid = str(d.get("video") or "").strip()
        if not cid or not vid:
            return self._send(400, {"ok": False, "msg": "id and video are required"})
        shelf.video_opened(cid, vid, bool(d.get("on", True)))
        shelf.save(force=True)
        return self._send(200, {"ok": True})

    def _post_channel_play(self, p, d):
        # No buffering, no Stremio, no heartbeat -- just the record
        # that it was opened, and whatever the provider says the TV
        # should hand to an external app.
        cid = str(d.get("id") or "").strip()
        vid = str(d.get("video") or "").strip()
        if not cid or not vid:
            return self._send(400, {"ok": False, "msg": "id and video are required"})
        try:
            r = gateway.channel_play(cid, vid)
        except contract.ProviderError as ex:
            return self._send(502, {"ok": False, "msg": ex.message})
        shelf.video_opened(cid, vid, True)
        shelf.save(force=True)
        return self._send(200, {"ok": True, "play": r})

    def _post_play_tv(self, p, d):
        bits = p.path[len("/api/play/tv/"):].split("/")
        if len(bits) != 3:
            return self._send(404, {"ok": False, "msg": "not found"})
        try:
            # The id stays a string: it is a provider-qualified
            # identifier now, and nothing guarantees it is numeric.
            tid, s, ep = self._id(bits[0]), int(bits[1]), int(bits[2])
        except ValueError:
            return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
        if not tid:
            return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
        jobid = f"tv:{tid}:{s}:{ep}"
        # Same race-refusal as the film route below, keyed by episode
        # rather than movie id.
        amid, aj = jobs.active_job()
        if amid is not None and amid == jobid:
            return self._send(202, {"ok": True, "msg": "already starting",
                                    "job": amid})
        q = urllib.parse.parse_qs(p.query)
        ap = q.get("autoplay", [None])[0]
        autoplay = config.AUTOPLAY_NEXT if ap is None else ap == "1"
        def resolve():
            entry = streams.get_stream_tv(tid, s, ep)
            return entry, entry.get("runtime") or 45
        return self._send(*jobs.start_play(jobid, resolve, autoplay=autoplay,
                                      start_s=watching._start_s(p.query)))

    def _post_play(self, p, d):
        tid = self._id(p.path.rsplit("/", 1)[-1])
        # Refuse rather than race. The loser of a two-job race does not
        # stop; it keeps buffering and then steals the TV when it lands.
        # Last request wins. A double-tap on the film already being
        # started attaches to that job rather than restarting it, but any
        # OTHER work -- an older job still probing, a film already on
        # screen -- is superseded here, so only one play is ever live.
        amid, aj = jobs.active_job()
        if amid is not None and amid == str(tid):
            return self._send(202, {"ok": True, "msg": "already starting",
                                    "job": amid})
        def resolve():
            # get_stream() already resolved the identity (and its
            # runtime, if the catalogue has one) to make this same
            # stream lookup -- nothing left to fetch again here.
            e = streams.get_stream(tid)
            return e, e.get("runtime")
        return self._send(*jobs.start_play(str(tid), resolve,
                                      start_s=watching._start_s(p.query)))

    def _post_bplay_tv(self, p, d):
        # The browser's counterpart to /api/play/tv/... above --
        # same id/season/episode shape and the same dedupe, but it
        # claims the player as owner="browser" against a session
        # token rather than the TV app, and its worker is
        # run_browser_job rather than run_play_job.
        bits = p.path[len("/api/bplay/tv/"):].split("/")
        if len(bits) != 3:
            return self._send(404, {"ok": False, "msg": "not found"})
        try:
            tid, s, ep = self._id(bits[0]), int(bits[1]), int(bits[2])
        except ValueError:
            return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
        if not tid:
            return self._send(400, {"ok": False, "msg": "bad id/season/episode"})
        jobid = f"tv:{tid}:{s}:{ep}"
        amid, aj = jobs.active_job()
        if amid is not None and amid == jobid:
            return self._send(202, {"ok": True, "msg": "already starting",
                                    "job": amid})
        caps = d.get("caps") or {}
        skip = d.get("skip") or []
        token = browser_play.new_token()
        # resolve() is the only place start_play() ever computes the
        # catalogue entry, and it runs INSIDE start_play's cancel
        # guard -- so the worker closure below cannot call
        # get_stream_tv() a second time without losing that guard.
        # Stashing the entry in this holder as a side effect of
        # resolve() is what lets the worker reach it, since by the
        # time the worker actually runs, resolve() has already been
        # called (start_play calls it synchronously before spawning
        # the thread).
        holder = {}
        def resolve():
            entry = streams.get_stream_tv(tid, s, ep)
            holder["entry"] = entry
            return entry, entry.get("runtime") or 45
        def worker(mid, picks, runtime_min, title, gen):
            browser_session.run_browser_job(mid, browser_session.browser_picks(holder["entry"], caps),
                            runtime_min, title, gen, token, caps, skip)
        start_s = watching._start_s(p.query)
        status, body = jobs.start_play(jobid, resolve, owner="browser",
                                  token=token, worker=worker,
                                  start_s=start_s)
        if status != 202:
            return self._send(status, body)
        # No absolute media URL here -- unlike /api/play/'s body,
        # which hands the TV app a URL it dials directly. The
        # browser gets its media URL only once run_browser_job has
        # actually decided how to serve it (see publish()), and it
        # is always a path relative to this same origin.
        # start_s is echoed rather than acted on: there is no command
        # channel to a browser, so the page seeks its own <video>
        # once the metadata is in.
        return self._send(202, {"ok": True, "job": jobid, "token": token,
                                "gen": jobs.job_get(jobid).get("gen"),
                                "start_s": start_s})

    def _post_bplay(self, p, d):
        # The browser's counterpart to /api/play/<id> above.
        tid = self._id(p.path.rsplit("/", 1)[-1])
        amid, aj = jobs.active_job()
        if amid is not None and amid == str(tid):
            return self._send(202, {"ok": True, "msg": "already starting",
                                    "job": amid})
        caps = d.get("caps") or {}
        skip = d.get("skip") or []
        token = browser_play.new_token()
        holder = {}
        def resolve():
            e = streams.get_stream(tid)
            holder["entry"] = e
            return e, e.get("runtime")
        def worker(mid, picks, runtime_min, title, gen):
            browser_session.run_browser_job(mid, browser_session.browser_picks(holder["entry"], caps),
                            runtime_min, title, gen, token, caps, skip)
        start_s = watching._start_s(p.query)
        status, body = jobs.start_play(str(tid), resolve, owner="browser",
                                  token=token, worker=worker,
                                  start_s=start_s)
        if status != 202:
            return self._send(status, body)
        return self._send(202, {"ok": True, "job": str(tid), "token": token,
                                "gen": jobs.job_get(str(tid)).get("gen"),
                                "start_s": start_s})

    def _post_bx_beat(self, p, d):
        # The browser's heartbeat: real currentTime and play/pause
        # state, on whatever cadence the page chooses.
        # browser_playing() treats a live heartbeat as "still
        # watching" even while paused, and regulate_hls() paces the
        # packager off _bx["pos"]/_bx["at"] this sets -- so a stale
        # or mismatched beat must change NOTHING, not even the
        # fields a live session's OWN beat would touch, or a
        # straggling request from a tab that has already moved on
        # could corrupt a newer session's pacing.
        token, gen = d.get("token"), d.get("gen")
        with core._lock:
            match = (token is not None and token == browser_session._bx["token"]
                     and gen == browser_session._bx["gen"])
        if not match:
            return self._send(409, {"ok": False, "stale": True})
        state = d.get("state")
        with core._lock:
            was = browser_session._bx["state"]
            browser_session._bx["at"] = time.time()
            browser_session._bx["state"] = state
            pos = d.get("pos")
            if pos is not None:
                try:
                    browser_session._bx["pos"] = float(pos)
                except (TypeError, ValueError):
                    pass
            mid, bpos, bdur = browser_session._bx["job"], browser_session._bx["pos"], browser_session._bx["dur"]
        # The shelf, outside the lock (see shelf_note): this beat is
        # the browser's only progress report, so it records where the
        # film got to exactly as the TV's heartbeat does -- "ended"
        # included, which is what marks it watched.
        if mid and state in ("playing", "paused", "ended"):
            watching.shelf_note(mid, bpos, bdur, state, force_save=(state != was))
        # A pause must never tear the job down -- only a lost
        # heartbeat does, via browser_playing()'s own staleness
        # check in browser_session.py. All a pause has to do here is stop the
        # packager burning CPU (and lead-time) on a viewer who has
        # stepped away, the same SIGSTOP/SIGCONT regulate_lead()
        # already uses for the TV's own transcode, with the same
        # _tc_flag(..., suspended=...) bookkeeping so /audio/-style
        # readers and regulate_hls()'s own resync never disagree
        # about whether this process is actually running.
        key = "bx:" + token
        name = config.BX_DIR + token
        if state in ("paused", "playing"):
            pid = transcode._ctr_pid(name)
            if pid:
                sig = "-STOP" if state == "paused" else "-CONT"
                subprocess.run(["docker", "exec", config.FFMPEG_CTR, "kill", sig, pid],
                              capture_output=True, timeout=15)
                transcode._tc_flag(key, suspended=(state == "paused"))
        return self._send(200, {"ok": True})

    def _post_bx_stop(self, p, d):
        # A browser session can be abandoned at any point: before
        # bx_begin() ever runs (still buffering/probing -- there is
        # no _bx session yet to tear down), during a live HLS
        # session, or after a direct-mode play that never touched
        # _bx at all. The token is the one thing that identifies
        # "this viewer's attempt" across every one of those states
        # -- a job id gets reused by a later replay of the same
        # title, but a token is minted fresh per attempt -- so both
        # halves of this route match on the token, independently of
        # each other, rather than on the job id.
        #
        # Must tolerate navigator.sendBeacon, which POSTs
        # Content-Type: text/plain and never reads the response --
        # _body() above parses JSON regardless of Content-Type, and
        # every reply here is a plain 200 the beacon will ignore.
        token = d.get("token")
        if not token:
            return self._send(200, {"ok": True})
        with core._lock:
            bx_match = browser_session._bx["token"] == token
        if bx_match:
            browser_session.bx_stop_all("Stopped")
        # Independently of the above: a job still buffering or
        # probing has no _bx session yet, so closing the tab during
        # that window must still be able to stand the worker down
        # -- otherwise it keeps holding the swarm open, and
        # active_job() keeps telling claim_owner() a browser is
        # still playing long after the viewer gave up and walked to
        # the TV, which then refuses to play with "Another device
        # is playing" for a film nobody is watching.
        with core._lock:
            amid = next((k for k, j in jobs._jobs.items()
                        if j.get("otoken") == token
                        and j.get("stage") in jobs.JOB_ACTIVE), None)
        if amid is not None:
            jobs.play_claim()   # the worker's next superseded(gen) check returns
            jobs.job_set(amid, stage="error", ok=False, msg="Playback abandoned")
        return self._send(200, {"ok": True})

    def _post_netcheck(self, p, d):
        # The probe saturates the link for a few seconds, so it must not
        # run against a film in progress -- it would starve the very
        # stream it is trying to characterise.
        if tvlink.tv_playback_state() in (2, 3):
            return self._send(409, {"ok": False,
                "msg": "Something is playing — stop it first, the test needs the link to itself."})
        if netprofile._net_busy["on"] or netprofile._cal["on"]:
            return self._send(202, {"ok": True, "msg": "already running"})
        # Runs in the background and reports through GET: a full
        # calibration takes minutes, and the button previously only ever
        # ran the HTTP probe -- which is why the "Optimising your
        # connection" progress was never reachable from the UI.
        # Pressing the button means "measure it again", so it always
        # calibrates rather than only topping up missing samples.
        threading.Thread(target=netprofile.net_full, kwargs={"force": True},
                         daemon=True).start()
        return self._send(202, {"ok": True, "msg": "started"})

    def _post_limits(self, p, d):
        if self._require_admin() is None:
            return
        vals = {}
        for k, lo in (("cap_mbps", config.SUSTAIN_MIN), ("cap_gb", 1.0)):
            v = d.get(k)
            try:
                v = float(v) if v not in (None, "") else 0.0
            except (TypeError, ValueError):
                return self._send(400, {"err": "%s must be a number" % k})
            # 0 clears the lid. Below the floor a lid would reject
            # every release there is, which is not a lid but an outage.
            if v != v or v < 0 or (v and v < lo) or v > 10000:
                return self._send(400, {"err": "%s must be 0 or at least %g" % (k, lo)})
            vals[k] = round(v, 1)
        return self._send(200, {"ok": True, **netprofile.set_limits(vals["cap_mbps"], vals["cap_gb"])})

    def _post_cancel(self, p, d):
        # Closing the film's popup means "I have changed my mind". The
        # job keeps buffering otherwise, holding the progress bar and the
        # play claim, and will eventually seize the TV for a film the
        # user walked away from. Deliberately does NOT touch the player:
        # a film already on screen is not an active job and must not be
        # interrupted by closing a different film's popup.
        amid, aj = jobs.active_job()
        # A browser tab fires this on popup-close, and once browser
        # playback exists that tab is not necessarily the one that
        # is actually playing -- a stray cancel from a second tab or
        # a delayed request must not kill somebody else's film.
        if (amid is not None and aj.get("owner") == "browser"
                and d.get("token") != aj.get("otoken")):
            return self._send(200, {"ok": False, "msg": "not yours"})
        if amid is None:
            with core._lock:
                inflight = jobs._play_inflight > 0
                if inflight:
                    jobs._cancel_gen += 1
            if inflight:
                # No job exists yet -- it's still in get_stream() -- so
                # there's nothing for active_job() to find. Bumping
                # _cancel_gen is the only way to reach it.
                print("play: cancelled while resolving streams", flush=True)
                return self._send(200, {"ok": True, "msg": "cancelled"})
            return self._send(200, {"ok": True, "msg": "nothing to cancel"})
        with core._lock:
            jobs._cancel_gen += 1  # also stands down a concurrent in-flight play
        jobs.play_claim()                      # the worker stands down
        jobs.job_set(amid, stage="error", ok=False, msg="Cancelled")
        print("play: %s cancelled by the user" % amid, flush=True)
        return self._send(200, {"ok": True, "msg": "cancelled", "job": amid})

    def _post_reconnect(self, p, d):
        # Nothing to reconnect to: the TV app is not something this
        # server dials, and adb is off.
        if not config.ADB_ENABLED:
            return self._send(200, {"ok": False, "msg": "phone remote is off",
                                    "state": "off"})
        hard = "hard" in urllib.parse.parse_qs(p.query)
        ok, msg = tvlink.adb_ready(hard=hard)
        return self._send(200, {"ok": ok, "msg": msg, "state": tvlink.adb_state()})

    def _post_stop(self, p, d):
        # Tell the app first: it is the thing with a film on screen, and
        # the command has to be queued before the job is retired or a
        # worker still in its handoff wait would race the stop.
        # Unconditionally, not only while the app is fresh: an app that
        # has just missed a poll is precisely the one that must not come
        # back and carry on playing a film that has been stopped here.
        # If it never comes back, the command expires by itself
        # (APP_CMD_TTL) instead of ambushing the next session.
        tvlink.app_cmd("stop")
        # An explicit stop is the one case the grace window must not
        # smooth over: the film is gone the moment this returns, so
        # nowplaying should say so, and the calibration guard should
        # not spend 45 s refusing on a memory of it.
        with core._lock:
            tvlink._app_last_play.update(state=None, at=0.0)
        jobs.play_claim()        # any in-flight job stands down
        amid, _ = jobs.active_job()
        if amid is not None:
            # Retire it now rather than waiting for its worker's next
            # checkpoint, or an immediate replay of the same film attaches
            # to a job that is already doomed and fails for no reason.
            jobs.job_set(amid, stage="error", ok=False, msg="Stopped")
        # Stop means stop regardless of who was playing -- a browser
        # session left live here would otherwise go on blocking a TV
        # play through claim_owner() after the human has already hit
        # the one button that is supposed to end it. bx_stop_all()
        # is the real teardown (kills the packager's ffmpeg, drops
        # the session directory); this used to only reset _bx's
        # fields by hand, which left that ffmpeg running -- a real
        # leak, since nothing else here ever reaped it.
        browser_session.bx_stop_all("Stopped")
        # Whatever the last heartbeat recorded is the resume point for
        # this film, and the viewer has just said they are done with
        # it for now: put it on disk rather than leave it to the
        # 30 s throttle of a heartbeat that is not coming.
        shelf.save(force=True)
        threading.Thread(target=torrents.cache_clear, daemon=True).start()
        ok, msg = tvlink.tv_stop()
        return self._send(200, {"ok": ok, "msg": msg})

    # Every route, tried in order: the first test that matches the path handles
    # the request, and nothing matching is a 404. Order matters only where one
    # path contains another -- /api/tv/{id}/season/ before /api/tv/, the
    # /tv/ forms of /api/stream/, /api/play/ and /api/bplay/ before the film
    # ones. A handler takes the parsed URL, and on POST the JSON body too.
    GET_ROUTES = (
        (lambda path: path == "/", _get_index),
        (lambda path: path in netprofile.STATIC, _get_static),
        (lambda path: path == "/api/setup/state", _get_setup_state),
        (lambda path: path == "/api/providers", _get_providers),
        (lambda path: path == "/api/genres", _get_genres),
        (lambda path: path == "/api/movies/progress", _get_movies_progress),
        (lambda path: path == "/api/shelf", _get_shelf),
        (lambda path: path == "/api/channel", _get_channel),
        (lambda path: path == "/api/channel/videos", _get_channel_videos),
        (lambda path: path == "/api/movies", _get_movies),
        (lambda path: path == "/api/search/stream", _get_search_stream),
        (lambda path: path == "/api/search", _get_search),
        (lambda path: path.startswith("/api/movie/"), _get_movie),
        (lambda path: path.startswith("/api/tv/") and "/season/" in path, _get_tv_season),
        (lambda path: path.startswith("/api/tv/"), _get_tv),
        (lambda path: path.startswith("/api/stream/tv/"), _get_stream_tv),
        (lambda path: path.startswith("/api/stream/"), _get_stream),
        (lambda path: path.startswith("/api/progress/"), _get_progress),
        (lambda path: path == "/api/bx/probes", _get_bx_probes),
        (lambda path: path.startswith("/src/"), _get_src),
        (lambda path: path.startswith("/t/"), _get_torrent),
        (lambda path: path.startswith("/audio/"), _get_audio),
        (lambda path: path.startswith("/hls/"), _get_hls),
        (lambda path: path == "/api/nowplaying", _get_nowplaying),
        (lambda path: path == "/api/hifi/players", _get_hifi_players),
        (lambda path: path == "/api/netcheck", _get_netcheck),
        (lambda path: path == "/api/health", _get_health),
    )

    POST_ROUTES = (
        (lambda path: path == "/api/setup/claim", _post_setup_claim),
        (lambda path: path == "/api/admin/login", _post_admin_login),
        (lambda path: path == "/api/admin/logout", _post_admin_logout),
        (lambda path: path == "/api/providers/preview", _post_providers_preview),
        (lambda path: path == "/api/providers/active", _post_providers_active),
        (lambda path: path.startswith("/api/providers/") and path.endswith("/config"), _post_provider_config),
        (lambda path: path.startswith("/api/providers/") and path.endswith("/test"), _post_provider_test),
        (lambda path: path.startswith("/api/providers/") and path.endswith("/enable"), _post_provider_enable),
        (lambda path: path.startswith("/api/providers/") and path.endswith("/remove"), _post_provider_remove),
        (lambda path: path == "/api/player/heartbeat", _post_heartbeat),
        (lambda path: path == "/api/hifi/delay", _post_hifi_delay),
        (lambda path: path == "/api/player/volume", _post_volume),
        (lambda path: path == "/api/shelf/fav", _post_shelf_fav),
        (lambda path: path == "/api/shelf/watched", _post_shelf_watched),
        (lambda path: path == "/api/shelf/drop", _post_shelf_drop),
        (lambda path: path == "/api/channel/follow", _post_channel_follow),
        (lambda path: path == "/api/channel/seen", _post_channel_seen),
        (lambda path: path == "/api/channel/opened", _post_channel_opened),
        (lambda path: path == "/api/channel/play", _post_channel_play),
        (lambda path: path.startswith("/api/play/tv/"), _post_play_tv),
        (lambda path: path.startswith("/api/play/"), _post_play),
        (lambda path: path.startswith("/api/bplay/tv/"), _post_bplay_tv),
        (lambda path: path.startswith("/api/bplay/"), _post_bplay),
        (lambda path: path == "/api/bx/beat", _post_bx_beat),
        (lambda path: path == "/api/bx/stop", _post_bx_stop),
        (lambda path: path == "/api/netcheck", _post_netcheck),
        (lambda path: path == "/api/limits", _post_limits),
        (lambda path: path == "/api/cancel", _post_cancel),
        (lambda path: path == "/api/reconnect", _post_reconnect),
        (lambda path: path == "/api/stop", _post_stop),
    )

class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        """A phone closing a tab or VLC hanging up mid-file is ordinary, not an
        error. The stdlib default prints a full traceback per disconnect, and
        /audio/ streams for hours -- the real failures were buried under dozens
        of these an hour."""
        ex = sys.exc_info()[1]
        if isinstance(ex, (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)
