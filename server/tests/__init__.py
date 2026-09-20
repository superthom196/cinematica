# The suite drives real heartbeats through server.py, which records watch
# progress. Point that at a throwaway file before anything imports server, or a
# test run writes stub titles into the real shelf.json beside the server.
import os
import tempfile

os.environ.setdefault(
    "CINEMATICA_SHELF",
    os.path.join(tempfile.mkdtemp(prefix="cinematica-shelf-"), "shelf.json"),
)
