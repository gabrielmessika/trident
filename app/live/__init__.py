"""Live collectors and snapshot helpers."""

__all__ = [
    "HyperliquidLiveCollector",
    "PodBFeatureBuilder",
    "LiveSnapshotBuilder",
    "LiveSnapshotWriter",
]


def __getattr__(name: str):
    if name == "HyperliquidLiveCollector":
        from app.live.collector import HyperliquidLiveCollector

        return HyperliquidLiveCollector
    if name == "PodBFeatureBuilder":
        from app.live.pod_b_feature_builder import PodBFeatureBuilder

        return PodBFeatureBuilder
    if name == "LiveSnapshotBuilder":
        from app.live.snapshot_builder import LiveSnapshotBuilder

        return LiveSnapshotBuilder
    if name == "LiveSnapshotWriter":
        from app.live.snapshot_writer import LiveSnapshotWriter

        return LiveSnapshotWriter
    raise AttributeError(name)
