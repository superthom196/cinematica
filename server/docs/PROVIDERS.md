# Providers

Providers supply the catalogue, title information and playable sources in Cinematica.
You add and manage them from the server's browser interface.

## Choose providers

| Role | What it supplies |
|---|---|
| Catalogue | Browse lists and search results |
| Metadata | Descriptions, artwork, ratings, seasons and episodes |
| Streams | Sources for a selected film or episode |

Each role has one active provider. A provider can fill several roles, and you can
choose a different provider for each. Cinematica does not combine several catalogues
or stream providers into one set of results.

There are two ways to add a provider:

- **Add-on URL:** paste a compatible Stremio `manifest.json` URL. Cinematica requests
  data from that service over HTTP. The add-on's code runs on its own server.
- **Python package:** upload an integration archive. Its code runs on your server
  on your server. `install.sh` runs it under its own `cinematica-provider`
  account, which is outside the Docker group, so it can't use Docker to take over
  the machine. That account is not a sandbox: the package still has the network
  and can read anything on the machine that every account can read. Only install
  packages from authors you trust. On an installation without that account
  (`CINEMATICA_PROVIDER_USER` unset), a package runs as the service account,
  whose Docker group is equivalent to root.

Cinematica bundles no integrations. Any service can supply any of the three
roles if it meets the compatibility requirements below.

### The channels role (optional)

A fourth, optional role: channels you follow, their uploads listed newest
first, and playback handed to an external app on the TV rather than played
through Cinematica itself. Setup never requires it -- a working install can
leave channels unset entirely, and it never affects the "configured" state
the other three roles determine.

| Op | Params | Result | Required? |
|---|---|---|---|
| `channels.resolve` | `query` | One channel | Required |
| `channels.details` | `id` | One channel | Required |
| `channels.latest` | `id` | `{videos, next}` | Required |
| `channels.play` | `id`, `video` | `{url, package, label}` | Required |
| `channels.videos` | `id`, `page` | `{videos, next}` | Optional |
| `channels.search` | `query`, `limit` | `{items}` (channels) | Optional |
| `channels.popular` | `limit` | `{items}` (channels) | Optional |

`resolve`, `details`, `latest` and `play` are what the role obliges. `videos`
(paged uploads beyond the latest page), `search` and `popular` are optional
within the role -- a channels provider may have no search index or no
separate "popular" concept at all -- and each install advertises which of
those three it actually answers through its own `provider.describe` reply's
`"channel_ops"` list.

`channels.play` hands back `{url, package, label}`: a playable URL, the app
to open it with, and a label for the button when it isn't. The TV opens
`package` if it names an installed app, or falls back to any app that
handles the URL when `package` is blank. No stream/buffer path is involved --
this bypasses Cinematica's own playback, proxying and probing entirely.

## Set up a provider

1. Sign in to the browser settings.
2. Add the manifest URL or upload the package.
3. Enter any requested configuration and use the test button.
4. Enable the provider and assign its roles.
5. Check browsing, title details and playback with the combination you selected.

For an add-on that requires an account or preferences, obtain its configured
manifest URL from the provider. That URL may contain credentials; keep it private.
The add-on's configuration page and Cinematica's settings serve different purposes:
Cinematica does not reproduce every add-on's own setup form.

A successful connection test checks communication with the provider. It does not
prove that every title has a playable source or that two providers use compatible
identifiers.

If no metadata provider is selected, Cinematica can use the catalogue provider
when it also supplies metadata. Otherwise, select a metadata provider separately.

## Manage existing providers

Use the browser controls to edit configuration, retest, enable or disable,
update, or remove a provider. Updating changes the installed definition or package;
assigning a role changes which provider Cinematica uses for that role.

Secret fields offer separate **Replace** and **Clear** actions. Leaving a stored
secret unchanged keeps its existing value.

Before replacing a provider package, back up the server's provider state. See
[backup locations](../INSTALL.md#where-everything-lives).

## When providers do not work together

Catalogue entries carry a title ID. The metadata and stream providers need to
understand that ID, or a shared external ID such as an IMDb identifier.

For example, a stream provider expecting `tt0133093` cannot automatically resolve
a catalogue's private identifier such as `my-library:42`. Cinematica does not have
a general title-matching service to translate between every provider's IDs.

IMDb IDs can be shared between providers. Private IDs generally require providers
that understand the same scheme. Changing the metadata provider alone may not solve
a mismatch: cross-provider metadata lookup currently supports shared IMDb IDs,
not arbitrary ID translation.

If a provider installs but a feature fails, check:

| Symptom | Possible cause |
|---|---|
| No search results | The add-on has no searchable catalogue, or the query returned no matches |
| Details or episodes fail | The metadata provider cannot resolve the catalogue's ID |
| No playable sources | No sources were returned, IDs do not match, or the returned formats are unsupported |
| Catalogue stops after one page | The add-on does not support the paging requests Cinematica sends |
| Connection test fails | Incorrect URL, credentials, service availability or network access |

The provider's status and error message are the starting point. Check the server
log if the interface does not give enough detail, and remove credentials before
sharing it.

## Add-on compatibility reference

Cinematica supports part of the Stremio add-on protocol. Accepting a manifest does
not imply support for every resource it declares.

### Resources and types

| Resource | Support |
|---|---|
| `catalog` | Browsing and search |
| `meta` | Title details and episode information |
| `stream` | Torrent and direct HTTP candidates |
| `subtitles` | Not supported |
| `addon_catalog` | Not supported |

Supported content types are `movie` and `series`. Resource declarations may be
strings or objects with a `name`. The adapter recognises those names; accepting an
object does not mean it implements every resource-level restriction in that object.

### Catalogue requests

The adapter sends these extra parameters:

| Parameter | Behaviour |
|---|---|
| `skip` | Sent after the first page as `(page - 1) × page_size` |
| `genre` | Sent for a selected genre declared by the catalogue |
| `search` | Sent to a catalogue that declares search support |

Both `extra` and the older `extraSupported` declarations are read. Other extra
parameters, cursor-based paging and add-on-specific sorting are not implemented.
The `skip` parameter is sent on later pages even if the catalogue did not declare it.

The adapter estimates whether more results exist from the response length. The
server removes repeated title IDs while collecting browse results and stops when
a page adds nothing new. This prevents an add-on that ignores `skip` from causing
endless requests, but cannot retrieve pages the add-on does not expose.

### Title and episode IDs

Cinematica stores a title as `<provider_id>:<local_id>` to keep providers' IDs
separate. A local ID matching `tt` followed by at least six digits is also recorded
as an IMDb ID.

For stream requests, the adapter:

1. Uses the local ID directly if it is an IMDb ID.
2. Otherwise uses the external IMDb ID when the manifest's top-level `idPrefixes`
   includes `tt`. It reports an unsupported-ID error if none is available.
3. Otherwise sends the local ID.

Series stream requests append the season and episode as `id:season:episode`.
Providers requiring another episode-ID format may not work.

### Stream formats

| Stream fields | Handling |
|---|---|
| `infoHash`, optional `fileIdx` | Torrent served through the local Stremio container |
| `url` beginning with `http://` or `https://` | Media fetched through Cinematica's HTTP proxy |
| `ytId` | Rejected |
| `externalUrl` | Rejected |

An HTTP URL alone does not guarantee playback. The response must be media the
playback path can handle; browser pages, DRM playback and arbitrary streaming
formats are not covered by URL acceptance.

Whether a candidate can play in a browser is decided the same way: by probing
the actual file with ffprobe and matching the result against what that specific
browser reports it can decode, not from the release name and not assumed from
the container or file extension. A candidate can therefore work on the TV or in
one browser and not another, or not reach the browser player at all when
nothing it returns is decodable there.

The adapter reads `behaviorHints.videoSize` when present. It can also extract sizes
and seeder counts from descriptive text. These estimates depend on what the add-on
reports; missing values remain unknown.

Quality preferences may relax when only a small number of sources are available.
Regional ranking also depends on the filters and vote information the catalogue
supports, so different providers can produce different browse orders.

## HTTP sources and credentials

For a direct HTTP source, the server stores the upstream URL and request headers.
It gives the TV, probe, transcoder and audio bridge a local `/src/<key>` URL. The
proxy adds the upstream headers when fetching media and forwards range requests
used for seeking.

Keep configured add-on URLs, account keys and signed media URLs private. Do not
include them in screenshots or issue reports.

An add-on may obtain its own data from other services. Changing providers changes
who Cinematica contacts, but does not establish where that provider's data originated.
Check the provider's documentation if its upstream sources matter to you.

## Python provider development

Packages contain a `manifest.json` and a Python entry file. The manifest declares
capabilities and configuration fields. The host loads the package in a separate
process and passes requests through the provider contract.

### A worked example

[`example-provider/`](example-provider/) is a complete package that fills all
three roles from a JSON list of films you host yourself. Copy the directory,
replace the library, keep the shapes. It is exercised by the server's test suite,
so it matches the contract the server actually enforces.

    example-provider/
      manifest.json           id, version, roles, configuration fields
      provider.py             the entry file: one function per operation
      library.example.json    the sample data it reads

The manifest is the smaller half:

```json
{
  "id": "example-library",
  "version": "1.0.0",
  "contract": 1,
  "capabilities": ["catalogue", "metadata", "streams"],
  "entry": "provider.py",
  "kinds": ["movie"],
  "config": [
    { "key": "base_url", "type": "url", "label": "Base URL", "required": true }
  ]
}
```

The host launches `python3 -m providers.host <package dir>` and sends it one JSON
request per line. It calls the function named after the operation with the dots
replaced by underscores, so `catalogue.browse` calls `catalogue_browse`. A
module-level `handle(op, config, params)` receives everything instead, if you
prefer a single entry point. An operation with neither is answered as
unsupported.

```python
from providers import contract

def catalogue_browse(config, params):
    """One page. params: kind, page (1-based), page_size, sort, filters."""
    page = max(1, int(params.get("page") or 1))
    size = max(1, int(params.get("page_size") or 20))
    rows = _library(config)[(page - 1) * size:page * size]
    # Only "id" and "kind" are required; every other field is optional.
    return {"items": [{"id": r["id"], "kind": "movie", "title": r["title"]} for r in rows],
            "has_more": page * size < len(_library(config))}

def streams_lookup(config, params):
    """Playable sources. params["identity"] carries local_id, title and external_ids."""
    row = _find(_library(config), params["identity"]["local_id"])
    return {"candidates": [{"transport": "http",
                            "url": "%s/%s" % (config["base_url"], row["path"]),
                            "source": "Example Library"}]}

def config_test(config, params):
    """The interface's test button. Make your smallest authenticated call here."""
    if not config.get("base_url"):
        raise contract.ProviderError(contract.E_CONFIG, "Base URL is not set.")
    return {"ok": True, "titles": len(_library(config))}
```

Operations a role obliges the package to answer are listed in `contract.py`'s
`ROLE_OPS`; `provider.describe` and `config.test` are required of every package.
Return plain dicts and lists. Every reply is normalised before the rest of the
server sees it, so unknown keys are dropped, types are coerced, and a shape that
cannot be made sense of is reported as an error against your provider rather than
raising somewhere else. Configuration arrives with each request, including
secrets; do not keep it in a module global.

Failures should be `contract.ProviderError(code, message)`. The code decides what
the interface shows and whether the answer is cached: `E_CONFIG` for something the
operator has not filled in, `E_NOTFOUND` for an id that does not exist there,
`E_UPSTREAM` for a service that failed, `E_UNSUPPORTED` for an operation the
package does not implement. Anything else raised becomes an internal error, which
is accurate but tells the operator nothing. Use `contract.redact()` on any message
quoting an exception, a URL or a configured value.

### Implementation references

- [`contract.py`](../providers/contract.py): manifest validation, operations and result formats.
- [`host.py`](../providers/host.py): entry loading and operation dispatch.
- [`runner.py`](../providers/runner.py): process lifecycle and request handling.

Together, `host.py` and the manifest schema in `contract.py` are the complete
specification a package has to satisfy. The example above is a starting point,
not a second specification: where the two disagree, the code decides.

The runner uses timeouts and error handling to contain routine provider failures.
It does not restrict the package's filesystem or network access.
