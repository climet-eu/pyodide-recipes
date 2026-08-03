import io
from pathlib import Path

import js
import pyodide
import pyodide_js

_RawHTTPBlobPyodide = pyodide.code.run_js("""
class _RawHTTPBlobPyodide {
    constructor(url, name, content_length, buffer_size, js) {
        this._url = url;
        this._content_length = content_length;

        this._buffer_start = 0;
        this._buffer = null;
        this._buffer_size = buffer_size;

        this._js = js;
    }

    get size() {
        return this._content_length;
    }

    slice(start=0, end=this.size, contentType="") {
        if (end <= start) {
            return new Blob(undefined, { type: contentType });
        }

        if (this._buffer !== null) {
            if (
                start >= this._buffer_start &&
                end <= (this._buffer_start + this._buffer.size)
            ) {
                return this._buffer.slice(
                    start - this._buffer_start,
                    end - this._buffer_start,
                    contentType,
                );
            }
        }

        const new_start = Math.max(0, start);
        const new_end = Math.max(Math.min(
            Math.max(end - 1, start + this._buffer_size - 1),
            this._content_length - 1,
        ), start + 1);

        const xhr = new XMLHttpRequest();
        xhr.responseType = "blob";
        xhr.open("GET", this._url, false);
        xhr.setRequestHeader("range", `bytes=${start}-${new_end}`);
        xhr.send(null);

        if (xhr.status == 200) {
            throw new this._js.FS.ErrnoError(
                this._js.ERRNO_CODES.EOPNOTSUPP
            );
        }

        const response = xhr.response;

        const real_start = new_start;
        const real_end = real_start + response.size;

        this._buffer_start = Math.max(
            real_start, real_end - this._buffer_size,
        );
        this._buffer = response.slice(this._buffer_start - real_start);

        return response.slice(0, end-start, contentType);
    }
} _RawHTTPBlobPyodide
""")

_DRIVE = Path("/drive")


def _get_content_length_encoding_accept_ranges_pyodide(
    url: str,
) -> tuple[None | int, None | str, None | str]:
    xhr = js.XMLHttpRequest.new()
    xhr.open("HEAD", url, False)
    xhr.send(None)

    content_length = xhr.getResponseHeader("content-length")
    content_length = int(content_length) if content_length else None

    accept_ranges = xhr.getResponseHeader("accept-ranges")
    accept_ranges = accept_ranges.lower() if accept_ranges else None

    content_encoding = xhr.getResponseHeader("content-encoding")
    content_encoding = content_encoding.lower() if content_encoding else None

    return (content_length, content_encoding, accept_ranges)


def _get_content_range_pyodide(
    url: str,
) -> None | str:
    xhr = js.XMLHttpRequest.new()
    xhr.open("GET", url, False)
    xhr.setRequestHeader("range", "bytes=0-1")
    xhr.send(None)

    if xhr.status != 206:
        return None

    if xhr.getResponseHeader("content-encoding") is not None:
        return None

    content_range = xhr.getResponseHeader("content-range")
    content_range = content_range.lower() if content_range else None

    return content_range


def mount_http_files(
    path: Path,
    filenames2urls: dict[str, str],
    buffer_size: int = io.DEFAULT_BUFFER_SIZE,
) -> None:
    path = Path(path).resolve(strict=False)

    blobs = []

    for name, url in filenames2urls.items():
        content_length, content_encoding, accept_ranges = (
            _get_content_length_encoding_accept_ranges_pyodide(url)
        )

        if content_encoding is not None:
            content_length = None

        if content_length is None and accept_ranges == "bytes":
            content_range = _get_content_range_pyodide(url)

            # Content-Range: <unit> <range-start>-<range-end>/<size>
            # Content-Range: <unit> <range-start>-<range-end>/*
            # Content-Range: <unit> */<size>
            if (
                content_range is not None
                and content_range.startswith("bytes ")
                and "/" in content_range
                and not content_range.endswith("/*")
            ):
                content_length = int(content_range.rsplit("/", 1)[-1])

        if content_length is None:
            raise IndexError(f"Unknown HTTP file length for '{url}'")

        if accept_ranges != "bytes":
            # let's hope for the best
            pass
            # raise TypeError(
            #     f"HTTP file at '{url}' does not support range requests"
            # )

        blobs.append(
            dict(
                name=name,
                data=_RawHTTPBlobPyodide.new(
                    url,
                    name,
                    content_length,
                    buffer_size,
                    pyodide_js,
                ),
            )
        )

    path_in_jupyterlite_drive = (path != _DRIVE) and path.is_relative_to(_DRIVE)

    # allow aliasing inside the JupyterLite /drive
    path.mkdir(parents=True, exist_ok=path_in_jupyterlite_drive)

    if len(blobs) > 0:
        pyodide_js.FS.mount(
            pyodide_js.FS.filesystems.WORKERFS,
            pyodide.ffi.to_js(
                dict(blobs=blobs),
                dict_converter=js.Object.fromEntries,
                create_pyproxies=False,
            ),
            str(path),
        )
