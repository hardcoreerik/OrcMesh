from dataclasses import dataclass

@dataclass(frozen=True)
class ChannelSummary:
    index: int
    name: str
    role: str
