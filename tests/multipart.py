"""A small multipart/form-data reader for the fake servers (stdlib only)."""

from __future__ import annotations

import re

import httpx

_NAME = re.compile(rb'name="([^"]*)"')
_FILENAME = re.compile(rb'filename="([^"]*)"')


def parse_multipart(
    request: httpx.Request,
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """(plain fields, file parts as name -> (file name, content)) of a multipart request."""
    content_type = request.headers["Content-Type"]
    boundary = content_type.split("boundary=", 1)[1].strip('"').encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in request.content.split(b"--" + boundary)[1:]:
        if part.startswith(b"--"):
            break
        head, _, body = part.strip(b"\r\n").partition(b"\r\n\r\n")
        name = _NAME.search(head)
        if name is None:
            continue
        filename = _FILENAME.search(head)
        if filename is None:
            fields[name.group(1).decode()] = body.decode()
        else:
            files[name.group(1).decode()] = (filename.group(1).decode(), body)
    return fields, files
