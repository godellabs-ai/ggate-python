"""DSPy adapter based on wrapping modules/callables."""

from __future__ import annotations

from ._common import sync_around, textify, wrap_method
from ..core.client import get_client


def instrument(*, module=None, sdk_client=None, framework: str = "dspy", **metadata):
    if module is None:
        return DSPyMonitor(sdk_client=sdk_client, framework=framework, **metadata)
    return wrap_module(module, sdk_client=sdk_client, framework=framework, **metadata)


class DSPyMonitor:
    def __init__(self, *, sdk_client=None, framework: str = "dspy", **metadata):
        self.sdk = sdk_client or get_client()
        self.framework = framework
        self.metadata = metadata

    def wrap(self, module):
        return wrap_module(module, sdk_client=self.sdk, framework=self.framework, **self.metadata)


def wrap_module(module, *, sdk_client=None, framework: str = "dspy", **metadata):
    sdk = sdk_client or get_client()

    def before(args, kwargs, fw):
        from ._common import extract_attachments_from_value
        attachments = extract_attachments_from_value(kwargs) + extract_attachments_from_value(args)
        text = textify({key: value for key, value in kwargs.items() if isinstance(value, str)} or args)
        sdk.scan_prompt(text, attachments=attachments, enforce=True, framework=fw, **metadata)

    def after(result, fw):
        text = textify(result)
        if text:
            sdk.scan_response(text, framework=fw, **metadata)

    for method in ("__call__", "forward", "aforward"):
        wrap_method(module, method, sync_around(before, after, framework))
    return DSPyModuleWrapper(module, before=before, after=after, framework=framework)


class DSPyModuleWrapper:
    def __init__(self, target, *, before, after, framework: str):
        self._target = target
        self._before = before
        self._after = after
        self._framework = framework

    def __getattr__(self, name):
        return getattr(self._target, name)

    def __call__(self, *args, **kwargs):
        self._before(args, kwargs, self._framework)
        result = self._target(*args, **kwargs)
        from ._common import maybe_awaitable_after

        return maybe_awaitable_after(result, lambda value: self._after(value, self._framework))
