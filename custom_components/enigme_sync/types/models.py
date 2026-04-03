from dataclasses import dataclass

@dataclass
class EnigmeDevice:
    path: str
    name: str
    state: str = "unknown"
