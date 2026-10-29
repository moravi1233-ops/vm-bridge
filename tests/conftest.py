"""Stable test ownership on clean Windows runners without any AI installation."""
import os
import uuid

os.environ.setdefault("VMBRIDGE_OWNER",str(uuid.uuid4()))
os.environ.setdefault("VMBRIDGE_OWNER_PID",str(os.getpid()))
