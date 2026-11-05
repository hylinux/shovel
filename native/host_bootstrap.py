"""Administrator-installed, isolated production Host entry point; never a trust bypass."""

import base64
import ctypes
import hashlib
import json
import os
import struct
import subprocess
import sys
import time
import winreg
from pathlib import Path

KEY = r"SOFTWARE\Shovel\WamTrust"
WRITERS = {
    "S-1-5-18",
    "S-1-5-32-544",
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
}


def protected(path: str, kind: int = 1, *, ancestor: bool = False) -> None:
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api.GetNamedSecurityInfoW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        *([ctypes.POINTER(ctypes.c_void_p)] * 5),
    ]
    api.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    api.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    api.ConvertSidToStringSidW.restype = ctypes.c_int
    api.GetAce.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    api.GetAce.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    owner, group, dacl, sacl, descriptor = (ctypes.c_void_p() for _ in range(5))
    if api.GetNamedSecurityInfoW(path, kind, 5, owner, group, dacl, sacl, descriptor):
        raise ValueError("Installation ACL unavailable.")

    def sid(pointer: int) -> str:
        result = ctypes.c_void_p()
        if not api.ConvertSidToStringSidW(pointer, result):
            raise ValueError("Installation SID unavailable.")
        try:
            return ctypes.wstring_at(result)
        finally:
            kernel.LocalFree(result)

    try:
        if not owner.value or not dacl.value or sid(owner.value) not in WRITERS:
            raise ValueError("Installation is not administrator controlled.")
        count = struct.unpack("<BBHHH", ctypes.string_at(dacl, 8))[3]
        mask = 0x500D0040 if ancestor else 0x500D0026 if kind == 4 else 0x500D0156
        for index in range(count):
            ace = ctypes.c_void_p()
            if not api.GetAce(dacl, index, ace) or not ace.value:
                raise ValueError("Invalid installation ACL.")
            kind_ace, flags, _, rights = struct.unpack("<BBHI", ctypes.string_at(ace, 8))
            if flags & 8 or not rights & mask:
                continue
            if kind_ace == 0:
                if sid(ace.value + 8) not in WRITERS:
                    raise ValueError("User-writable installation.")
            elif kind_ace not in (1, 6):
                raise ValueError("Unsupported writable installation ACE.")
    finally:
        kernel.LocalFree(descriptor)


def unique(items: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(items)
    if len(result) != len(items):
        raise ValueError("Duplicate manifest keys.")
    return result


def machine_state() -> dict:
    protected("MACHINE\\" + KEY, 4)
    with winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE, KEY, access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    ) as key:
        raw = winreg.QueryValueEx(key, "State")[0]
    if not isinstance(raw, str) or len(raw) > 1024 * 1024:
        raise ValueError("Invalid machine trust state.")
    state = json.loads(raw, object_pairs_hook=unique)
    active = state["active"]
    if (
        set(state)
        != {
            "schema",
            "storeRoot",
            "generation",
            "highestRevision",
            "signers",
            "revokedSigners",
            "revokedEnvelopes",
            "active",
        }
        or state["schema"] != 2
        or type(state["generation"]) is not int
        or state["generation"] < 1
        or active is None
        or set(active) != {"revision", "envelopeSha256", "signerSha256"}
        or type(active["revision"]) is not int
        or active["revision"] < 1
        or type(state["highestRevision"]) is not int
        or active["revision"] != state["highestRevision"]
        or active["signerSha256"] not in state["signers"]
        or active["signerSha256"] in state["revokedSigners"]
        or active["envelopeSha256"] in state["revokedEnvelopes"]
        or len(active["envelopeSha256"]) != 64
        or any(c not in "0123456789abcdef" for c in active["envelopeSha256"])
        or any(
            not isinstance(group, list)
            or len(group) > 10000
            or len(set(group)) != len(group)
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
                for value in group
            )
            for group in (state["signers"], state["revokedSigners"], state["revokedEnvelopes"])
        )
        or not Path(state["storeRoot"]).is_absolute()
        or state["storeRoot"].startswith("\\\\")
    ):
        raise ValueError("Machine trust is inactive or revoked.")
    return state


def protected_ancestors(path: Path) -> None:
    for parent in path.parents:
        if parent.is_symlink() or parent.is_junction():
            raise ValueError("Installation ancestor reparse point.")
        protected(str(parent), ancestor=True)


def require_current_installation(content: dict) -> None:
    state = machine_state()
    if (
        state["generation"] != content["_generation"]
        or state["active"]["envelopeSha256"] != content["_digest"]
        or content["validUntil"] <= time.time()
    ):
        raise ValueError("Installed Host authority changed or expired.")


def load() -> dict:
    state = machine_state()
    active = state["active"]
    path = Path(state["storeRoot"]) / "policies" / (active["envelopeSha256"] + ".json")
    digest = active["envelopeSha256"]
    revision = active["revision"]
    protected_ancestors(path)
    protected(str(path))
    with path.open("rb") as stream:
        envelope = stream.read(8 * 1024 * 1024 + 1)
    if len(envelope) > 8 * 1024 * 1024 or hashlib.sha256(envelope).hexdigest() != digest:
        raise ValueError("Installation manifest integrity failure.")
    content = json.loads(
        base64.b64decode(json.loads(envelope, object_pairs_hook=unique)["payload"], validate=True),
        object_pairs_hook=unique,
    )
    if content["revision"] != revision or content["validUntil"] <= time.time():
        raise ValueError("Installation policy expired or changed.")
    root = Path(content["root"])
    if (
        not root.is_absolute()
        or root != Path(state["storeRoot"]) / "packages" / str(revision)
        or content.get("runtime") is None
    ):
        raise ValueError("Installation path must be absolute.")
    protected_ancestors(root)
    expected = content["files"]
    actual = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("Installation reparse point.")
        protected(str(directory))
        for path in directory.iterdir():
            if path.is_symlink() or path.is_junction():
                raise ValueError("Installation reparse point.")
            if path.is_dir():
                pending.append(path)
            else:
                actual.add(str(path.relative_to(root)))
            if len(actual) > 100000:
                raise ValueError("Installation inventory too large.")
    if actual != set(expected) or len({name.casefold() for name in expected}) != len(expected):
        raise ValueError("Installation inventory changed.")
    for name, digest_file in expected.items():
        if (
            Path(name).is_absolute()
            or "/" in name
            or ":" in name
            or any(part in ("", ".", "..") for part in name.split("\\"))
            or any(part.endswith((" ", ".")) or os.path.isreserved(part) for part in name.split("\\"))
        ):
            raise ValueError("Invalid installation path.")
        path = root
        for part in name.split("\\"):
            path /= part
            if path.is_symlink() or path.is_junction():
                raise ValueError("Installation reparse point.")
        protected(str(path))
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != digest_file:
                raise ValueError("Installation file integrity failure.")
    if state != machine_state():
        raise ValueError("Machine trust changed during verification.")
    content["_digest"] = digest
    content["_generation"] = state["generation"]
    return content


def main() -> None:
    if (
        not sys.flags.isolated
        or not sys.flags.no_site
        or not sys.flags.dont_write_bytecode
        or len(sys.argv) != 1
    ):
        raise ValueError("Host requires isolated installed bootstrap.")
    content = load()
    sys.dont_write_bytecode = True
    root = Path(content["root"])
    if (
        Path(sys.executable) != root / content["python"]
        or Path(__file__).resolve() != root / content["bootstrap"]
    ):
        raise ValueError("Unexpected Host entry point.")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "TEMP", "TMP"}
    }
    # Native image and dependencies are in the machine-pinned installation inventory.
    outcome = subprocess.run(  # noqa: S603
        [str(root / content["native"]), "attest"],
        env=environment,
        cwd=root,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if outcome.returncode or json.loads(outcome.stdout)["status"] != "trusted":
        raise ValueError("Native installation attestation failed.")
    current = load()
    if (current["_digest"], current["_generation"]) != (content["_digest"], content["_generation"]):
        raise ValueError("Installation changed during attestation.")
    sys.path[:] = [str(root / path) for path in content["modulePaths"]]
    dll_handles = [os.add_dll_directory(str(root / path)) for path in content["runtime"]["dllDirectories"]]
    from shovel.identity.native_wam import configure_installed_host

    configure_installed_host(content, load)
    from shovel.enterprise.organization import OrganizationAnchor, configure_organization_host

    def organization_policy():
        require_current_installation(content)
        trust_key = r"SOFTWARE\Shovel\EnterpriseTrust"
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                trust_key,
                access=winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            )
        except FileNotFoundError:
            return None
        with key:
            protected("MACHINE\\" + trust_key, 4)
            policy_path = winreg.QueryValueEx(key, "PolicyPath")[0]
            anchor = OrganizationAnchor(
                organization_id=winreg.QueryValueEx(key, "OrganizationId")[0],
                revision=winreg.QueryValueEx(key, "Revision")[0],
                certificate_sha256=winreg.QueryValueEx(key, "SignerCertificateSha256")[0],
                envelope_sha256=winreg.QueryValueEx(key, "PolicySha256")[0],
            )
        protected(policy_path)
        if not Path(policy_path).is_absolute():
            raise ValueError("Organization policy path must be absolute.")
        with Path(policy_path).open("rb") as stream:
            envelope = stream.read(1024 * 1024 + 1)
        return anchor, envelope

    configure_organization_host(organization_policy)
    from shovel.cli import app

    sys.argv[:] = ["shovel", "run", "prd"]
    try:
        app()
    finally:
        for handle in dll_handles:
            handle.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print('{"scope":"wam_installation","status":"unavailable","production_ready":false}', file=sys.stderr)
        raise SystemExit(3) from None
