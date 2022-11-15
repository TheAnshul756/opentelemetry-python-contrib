from logging import getLogger
from time import time_ns
from timeit import default_timer
from typing import Collection
import types
from sys import exc_info

from opentelemetry.util.http import parse_excluded_urls, get_excluded_urls
import cherrypy
from opentelemetry.instrumentation.cherrypy.package import _instruments
from opentelemetry.instrumentation.instrumentor import BaseInstrumentor
from opentelemetry.instrumentation.utils import _start_internal_or_server_span
import opentelemetry.instrumentation.wsgi as otel_wsgi
from opentelemetry.instrumentation.propagators import (
    get_global_response_propagator,
)
from opentelemetry import trace, context
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.instrumentation.cherrypy.version import __version__
from opentelemetry.metrics import get_meter


_logger = getLogger(__name__)
_excluded_urls_from_env = get_excluded_urls("CHERRYPY")

_TOOL_NAME = 'opentelemetry-cherrypy-tool'

class _InstrumentationHook(cherrypy.Tool):
    def __init__(self, **kwargs):
        tracer_provider = kwargs.pop('tracer_provider', None)
        meter_provider = kwargs.pop('metr_provider', None)
        self._otel_tracer = trace.get_tracer(__name__, __version__, tracer_provider)
        otel_meter = get_meter(__name__, __version__, meter_provider)
        self.duration_histogram = otel_meter.create_histogram(
            name="http.server.duration",
            unit="ms",
            description="measures the duration of the inbound HTTP request",
        )
        self.active_requests_counter = otel_meter.create_up_down_counter(
            name="http.server.active_requests",
            unit="requests",
            description="measures the number of concurrent HTTP requests that are currently in-flight",
        )
        self.request_hook = kwargs.pop('request_hook', None)
        self.response_hook = kwargs.pop('response_hook', None)
        excluded_urls = kwargs.pop('excluded_urls', None)
        self._otel_excluded_urls = (_excluded_urls_from_env if excluded_urls is None else parse_excluded_urls(excluded_urls))
        self._is_instrumented_by_opentelemetry = True
        super().__init__("on_start_resource", self._on_start_resource_hook, name=_TOOL_NAME, priority=0)
    
    def _setup(self):
        super()._setup()
        cherrypy.serving.request.hooks.attach("before_finalize", self._before_finalize_hook,
                                              priority=100)
        cherrypy.serving.request.hooks.attach("on_end_resource", self._on_end_resource_hook,
                                              priority=100)
        cherrypy.serving.request.hooks.attach("after_error_response", self._after_error_response_hook,
                                              priority=100)
        cherrypy.serving.request.hooks.attach("on_end_request", self._on_end_request_hook,
                                              priority=100)
    
    def _on_start_resource_hook(self):
        environ = cherrypy.serving.request.wsgi_environ
        if self._otel_excluded_urls and self._otel_excluded_urls.url_disabled(environ.get('PATH_INFO', '/')):
            return
        
        if not self._is_instrumented_by_opentelemetry:
            return
        
        start_time = time_ns()
        self.span, self.token = _start_internal_or_server_span(
            tracer=self._otel_tracer,
            span_name=otel_wsgi.get_default_span_name(environ),
            start_time=start_time,
            context_carrier=environ,
            context_getter=otel_wsgi.wsgi_getter,
        )
        if self.request_hook:
            self.request_hook(self.span, environ)
        attributes = otel_wsgi.collect_request_attributes(environ)
        self.active_requests_count_attrs = (
            otel_wsgi._parse_active_request_count_attrs(attributes)
        )
        self.duration_attrs = otel_wsgi._parse_duration_attrs(attributes)
        self.active_requests_counter.add(1, self.active_requests_count_attrs)

        if self.span.is_recording():
            for key, value in attributes.items():
                self.span.set_attribute(key, value)
            if self.span.is_recording() and self.span.kind == trace.SpanKind.SERVER:
                custom_attributes = (
                    otel_wsgi.collect_custom_request_headers_attributes(environ)
                )
                if len(custom_attributes) > 0:
                    self.span.set_attributes(custom_attributes)

        self.activation = trace.use_span(self.span, end_on_exit=True)
        self.activation.__enter__()
        self.start = default_timer()
        self.exception = None
        

    def _before_finalize_hook(self):
        if self._otel_excluded_urls and self._otel_excluded_urls.url_disabled(cherrypy.serving.request.wsgi_environ.get('PATH_INFO', '/')):
            return
        if not self._is_instrumented_by_opentelemetry:
            return
        propagator = get_global_response_propagator()
        if propagator:
            propagator.inject(cherrypy.serving.response.headers, setter=otel_wsgi.default_response_propagation_setter)
    
    def _on_end_resource_hook(self):
        if self._otel_excluded_urls and self._otel_excluded_urls.url_disabled(cherrypy.serving.request.wsgi_environ.get('PATH_INFO', '/')):
            return
        if not self._is_instrumented_by_opentelemetry:
            return
        if self.span:
            otel_wsgi.add_response_attributes(self.span, cherrypy.serving.response.status, cherrypy.serving.response.headers)
            status_code = otel_wsgi._parse_status_code(cherrypy.serving.response.status)
            if status_code is not None:
                self.duration_attrs[SpanAttributes.HTTP_STATUS_CODE] = status_code
            if self.span.is_recording() and self.span.kind == trace.SpanKind.SERVER:
                custom_attributes = otel_wsgi.collect_custom_response_headers_attributes(cherrypy.serving.response.headers.items())
                if len(custom_attributes) > 0:
                    self.span.set_attributes(custom_attributes)
    
    def _after_error_response_hook(self):
        if self._otel_excluded_urls and self._otel_excluded_urls.url_disabled(cherrypy.serving.request.wsgi_environ.get('PATH_INFO', '/')):
            return
        if not self._is_instrumented_by_opentelemetry:
            return
        _, self.exception, _ = exc_info()

    def _on_end_request_hook(self):
        if self._otel_excluded_urls and self._otel_excluded_urls.url_disabled(cherrypy.serving.request.wsgi_environ.get('PATH_INFO', '/')):
            return
        if not self._is_instrumented_by_opentelemetry:
            return
        if self.exception is None:
            self.activation.__exit__(None, None, None)
        else:
            self.activation.__exit__(
                type(self.exception),
                self.exception,
                getattr(self.exception, "__traceback__", None),
            )
        if self.token is not None:
            context.detach(self.token)
        duration = max(round((default_timer() - self.start) * 1000), 0)
        self.duration_histogram.record(duration, self.duration_attrs)
        self.active_requests_counter.add(-1, self.active_requests_count_attrs)

class CherryPyInstrumentor(BaseInstrumentor):
    """An instrumentor for FastAPI

    See `BaseInstrumentor`
    """

    def instrumentation_dependencies(self) -> Collection[str]:
        return _instruments

    def _instrument(self, **kwargs):
        self.original_expose = cherrypy.expose
        self.otel_tool = _InstrumentationHook(**kwargs)
        setattr(cherrypy.tools, _TOOL_NAME, self.otel_tool)
        tool_decorator = self.otel_tool()
        def _Instrumented_expose(func=None, alias=None):
            decoratable_types = types.FunctionType, types.MethodType, type,
            if func is None or not isinstance(func, decoratable_types):
                expose_callable = self.original_expose(func, alias)
                def _Instrumented_expose_callable(func):
                    func = tool_decorator(func)
                    return expose_callable(func)
                return _Instrumented_expose
            else:
                func = tool_decorator(func)
                return self.original_expose(func, alias)
        cherrypy.expose = _Instrumented_expose

    def _uninstrument(self, **kwargs):
        self.otel_tool._is_instrumented_by_opentelemetry = False
