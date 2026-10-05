"""Collect only Q22's local Parquet/dataset runtime, never all Arrow integrations."""
from PyInstaller.utils.hooks import collect_dynamic_libs

hiddenimports = [
    "pyarrow.lib", "pyarrow._compute", "pyarrow._fs", "pyarrow._dataset", "pyarrow._acero",
    "pyarrow._parquet", "pyarrow._dataset_parquet", "pyarrow._csv", "pyarrow._json",
]
# Dependency scanning resolves platform-specific transitive DLLs/dylibs. Avoid
# Arrow Flight, cloud stores and data-format adapters the application never uses.
excludedimports = [
    "pyarrow._azurefs", "pyarrow._gcsfs", "pyarrow._s3fs", "pyarrow._hdfs",
    "pyarrow._flight", "pyarrow.flight", "pyarrow._cuda", "pyarrow.cuda",
    "pyarrow._orc", "pyarrow.orc", "pyarrow._dataset_orc", "pyarrow._substrait",
    "pyarrow.substrait", "pyarrow._parquet_encryption", "pyarrow._dataset_parquet_encryption",
    "pyarrow.tests",
]
binaries = [item for item in collect_dynamic_libs("pyarrow")
            if not any(part in item[0].lower() for part in ("flight", "substrait", "cuda"))]
