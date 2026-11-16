"""Protocol primitives for the display-side UART emulator."""

from .checksum import calculate_sum16, sum16_bytes, validate_aa_frame, validate_response
from .frames import Dataset, ResponseFrame
from .parser import FrameKind, ParsedFrame, StreamParser
from .replay import ReplayEngine, ReplayMode, ReplayProfile, ReplayStep, RequestType
from .live_analysis import BitTransition, ByteChange, LiveChangeAnalyzer, LiveFrameDifference
from .framing import (
    FrameByteMetadata,
    FrameByteRole,
    FrameLayout,
    frame_byte_metadata,
    frame_layout,
    frame_metadata,
)
from .display_role import (
    AAPayloadPreset,
    DisplayRequest,
    DisplayRequestScheduler,
    DisplayRoleConfig,
    DisplayRoleController,
    EmulatorRole,
    build_aa_frame,
    direction_labels,
)

__all__ = [
    "Dataset",
    "FrameKind",
    "ParsedFrame",
    "ResponseFrame",
    "ReplayEngine",
    "ReplayMode",
    "ReplayProfile",
    "ReplayStep",
    "RequestType",
    "BitTransition",
    "ByteChange",
    "LiveChangeAnalyzer",
    "LiveFrameDifference",
    "FrameByteMetadata",
    "FrameByteRole",
    "FrameLayout",
    "AAPayloadPreset",
    "DisplayRequest",
    "DisplayRequestScheduler",
    "DisplayRoleConfig",
    "DisplayRoleController",
    "EmulatorRole",
    "StreamParser",
    "calculate_sum16",
    "sum16_bytes",
    "validate_aa_frame",
    "validate_response",
    "build_aa_frame",
    "direction_labels",
    "frame_byte_metadata",
    "frame_layout",
    "frame_metadata",
]
