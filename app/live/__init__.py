"""Live collectors and snapshot helpers."""

__all__ = [
    "HyperliquidLiveCollector",
    "LiveSnapshotBuilder",
    "LiveSnapshotWriter",
]


def __getattr__(name: str):
    if name == "HyperliquidLiveCollector":
        from app.live.collector import HyperliquidLiveCollector

        return HyperliquidLiveCollector
    if name == "LiveSnapshotBuilder":
        from app.live.snapshot_builder import LiveSnapshotBuilder

        return LiveSnapshotBuilder
    if name == "LiveSnapshotWriter":
        from app.live.snapshot_writer import LiveSnapshotWriter

        return LiveSnapshotWriter
    raise AttributeError(name)
