"""Release-version helpers shared by packaging and focused validation."""


def windows_version(value: str) -> tuple[int, int, int, int]:
    """Map DeckRelay's public semantic beta version to a Windows file version."""
    release, separator, prerelease = value.partition("-")
    parts = release.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError("Release version must contain three numeric components")
    major, minor, patch = (int(part) for part in parts)
    if not separator:
        return major, minor, patch, 0
    if not prerelease.startswith("beta.") or not prerelease.removeprefix("beta.").isdigit():
        raise ValueError("Prerelease version must use the beta.<number> form")
    return major, minor, patch, int(prerelease.removeprefix("beta."))
