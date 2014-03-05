# -*- coding: utf-8 -*-
"""
    scap
    ~~~~
    Wikimedia's MediaWiki deployment script. Deploys MediaWiki code and
    configuration to a group of servers via SSH and rsync.

"""
from .main import MWVersionsInUse
from .main import Scap
from .main import SyncCommon
from .main import SyncWikiversions

from . import log

__all__ = (
    'MWVersionsInUse',
    'Scap',
    'SyncCommon',
    'SyncWikiversions',
)

any((
    MWVersionsInUse,
    Scap,
    SyncCommon,
    SyncWikiversions))  # Ignore unused import warning

log.setup_loggers()
