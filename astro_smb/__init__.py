"""Astro SMB Tool 核心库(基于 impacket,SMB 2/3,匿名访问)。"""

from astro_smb.client import (
    AstroSmbClient,
    DirStat,
    RemoteEntry,
    ShareInfo,
    SmbClientError,
    TransferCancelled,
    VolumeInfo,
    split_remote_path,
)

__all__ = [
    "AstroSmbClient",
    "DirStat",
    "RemoteEntry",
    "ShareInfo",
    "SmbClientError",
    "TransferCancelled",
    "VolumeInfo",
    "split_remote_path",
]

__version__ = "0.1.0"
