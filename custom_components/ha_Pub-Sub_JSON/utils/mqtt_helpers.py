def path_to_topic(path: str, base: str) -> str:
    """Convertit BR.CRYPTE en enigme/BR/CRYPTE."""
    return f"{base}/{path.replace('.', '/')}"
