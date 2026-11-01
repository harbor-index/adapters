from __future__ import annotations

import asyncio
import inspect

import nltk
import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "asyncio: execute async test coroutine in an event loop"
    )


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        funcargs = pyfuncitem.funcargs
        testargs = {arg: funcargs[arg] for arg in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(pyfuncitem.obj(**testargs))
        return True
    return None


@pytest.fixture(scope="session", autouse=True)
def nltk_tokenizer_data(tmp_path_factory: pytest.TempPathFactory):
    data_dir = tmp_path_factory.mktemp("nltk_data")
    for package in ("punkt", "punkt_tab"):
        nltk.download(package, download_dir=data_dir, quiet=True, raise_on_error=True)

    nltk.data.path.insert(0, str(data_dir))
    yield
    nltk.data.path.remove(str(data_dir))
