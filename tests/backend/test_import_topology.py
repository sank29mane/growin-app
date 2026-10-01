"""Regression coverage for the repository's mixed backend import contract."""

import importlib


def test_package_and_legacy_flat_backend_imports_resolve():
    modules = (
        "backend.analytics_db",
        "analytics_db",
        "backend.coreml.fast_gmm",
        "coreml.fast_gmm",
        "backend.features.online_vol",
        "features.online_vol",
        "backend.mlx.adapter_manager",
        "mlx.adapter_manager",
        "backend.simulation.engine",
        "simulation.engine",
    )

    for module_name in modules:
        assert importlib.import_module(module_name) is not None
