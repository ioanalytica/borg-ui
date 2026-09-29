import re
from typing import Any, Mapping

from app.utils.ssh_paths import apply_ssh_command_prefix


def strip_ssh_url_path(path: str) -> str:
    if not path.startswith("ssh://"):
        return path

    match = re.match(r"ssh://[^/]+(/.*)", path)
    if match:
        return match.group(1)
    return path.split("/", 3)[-1] if "/" in path else path


def ssh_repository_directory(path: str, *, borg_version: int = 1) -> str:
    """The directory on the host that an ssh:// repository URL names, for
    what reaches the files without Borg (a mount, a copy).

    Borg 1 reads the path after the host as absolute. Borg 2 reads
    `ssh://host//abs` as absolute and `ssh://host/rel` as relative to the
    login directory of the SSH user, which nothing here knows: such a URL
    names no directory that can be handed on, and taking its text for an
    absolute path would name another one. Raises ValueError for it.
    """
    tail = strip_ssh_url_path(path)
    if borg_version != 2 or not path.startswith("ssh://"):
        return tail or "/"
    absolute, directory = _borg2_path(tail, from_url=True)
    if not absolute:
        raise ValueError(
            "this Borg 2 repository is addressed relative to the login "
            "directory of the SSH user (ssh://host/path); its directory on "
            "the host is only known for the absolute form, ssh://host//path"
        )
    return directory


def _borg2_path(tail: str, *, from_url: bool) -> tuple[bool, str]:
    """(absolute, path) of what follows the host in a Borg 2 URL, or of a
    plain path.

    Borg 2 reads `ssh://host/rel` as relative to the login directory and
    `ssh://host//abs` as absolute. A URL tail carries the separator: `//abs`
    is absolute, `/rel` relative. A plain path is absolute with a leading
    slash, except Borg 1's spelling of the login directory, `/./rel`.
    """
    if from_url:
        if tail.startswith("//"):
            return True, "/" + tail.lstrip("/")
        return False, tail[1:] if tail.startswith("/") else tail
    if tail in (".", "/.") or tail.startswith("/./"):
        return False, tail.lstrip("/")
    if tail.startswith("/"):
        return True, "/" + tail.lstrip("/")
    return False, tail


def build_ssh_repository_path(
    raw_path: str,
    connection_details: Mapping[str, Any],
    *,
    borg_version: int = 1,
    stored_path: str | None = None,
) -> str:
    """The ssh:// URL of a repository on a connection, in the syntax of the
    Borg major that will read it.

    Borg 1 reads the path after the host as absolute (`/./x` names `x` in the
    login directory). Borg 2 reads it as relative to the login directory and
    takes a second slash for an absolute path. The same text therefore names
    two places, and an absolute path written the Borg 1 way lands under the
    login directory.

    `raw_path` is a URL, which keeps the form it came in, or a plain path,
    where a leading slash means absolute. `stored_path` is the URL the
    repository has now: the repository form sends the tail of that URL back
    as the path, so a path equal to it is the stored URL unchanged, not a
    plain path.
    """
    base = (
        f"ssh://{connection_details['username']}@"
        f"{connection_details['host']}:{connection_details['port']}/"
    )
    repo_path = strip_ssh_url_path(raw_path)
    ssh_path_prefix = connection_details.get("ssh_path_prefix")

    if borg_version != 2:
        if ssh_path_prefix:
            repo_path = apply_ssh_command_prefix(repo_path, str(ssh_path_prefix))
        return base + repo_path.lstrip("/")

    from_url = raw_path.startswith("ssh://") or (
        stored_path is not None
        and stored_path.startswith("ssh://")
        and strip_ssh_url_path(stored_path) == raw_path
    )
    absolute, path = _borg2_path(repo_path, from_url=from_url)
    if ssh_path_prefix and absolute:
        # the prefix belongs to paths on the host, not to the login directory
        path = apply_ssh_command_prefix(path, str(ssh_path_prefix))
        absolute, path = _borg2_path(path, from_url=False)
    return base + ("/" + path.lstrip("/") if absolute else path)
