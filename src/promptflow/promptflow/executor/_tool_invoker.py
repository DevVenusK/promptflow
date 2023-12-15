# ---------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# ---------------------------------------------------------

import docutils.nodes
import functools
import inspect
import logging
import os
import threading
import time
import uuid
from docutils.core import publish_doctree
from contextvars import ContextVar
from functools import partial
from logging import WARNING
from pathlib import Path
from typing import Callable, Optional

from promptflow._core._errors import ToolExecutionError, UnexpectedError
from promptflow._core.cache_manager import AbstractCacheManager, CacheInfo, CacheResult
from promptflow._core.operation_context import OperationContext
from promptflow._core.run_tracker import RunTracker
from promptflow._core.thread_local_singleton import ThreadLocalSingleton
from promptflow._core.tracer import Tracer
from promptflow._utils.logger_utils import flow_logger, logger
from promptflow._utils.thread_utils import RepeatLogTimer
from promptflow._utils.utils import generate_elapsed_time_messages
from promptflow.contracts.flow import InputAssignment, Node, ToolSource
from promptflow.contracts.run_info import RunInfo
from promptflow.executor._tool_resolver import ToolResolver
from promptflow.exceptions import PromptflowException


class DefaultToolInvoker(ThreadLocalSingleton):
    CONTEXT_VAR_NAME = "Invoker"
    context_var = ContextVar(CONTEXT_VAR_NAME, default=None)

    def __init__(
        self,
        name,
        run_tracker: RunTracker,
        cache_manager: AbstractCacheManager,
        working_dir: Optional[Path] = None,
        connections: Optional[dict] = None,
        run_id=None,
        flow_id=None,
        line_number=None,
        variant_id=None,
    ):
        self._name = name
        self._run_tracker = run_tracker
        self._cache_manager = cache_manager
        self._working_dir = working_dir
        self._connections = connections or {}
        self._run_id = run_id or str(uuid.uuid4())
        self._flow_id = flow_id or self._run_id
        self._line_number = line_number
        self._variant_id = variant_id
        self._assistant_tools = {}

    @classmethod
    def start_invoker(
        cls,
        name,
        run_tracker: RunTracker,
        cache_manager: AbstractCacheManager,
        working_dir: Optional[Path] = None,
        connections: Optional[dict] = None,
        run_id=None,
        flow_id=None,
        line_number=None,
        variant_id=None
    ):
        invoker = cls(name, run_tracker, cache_manager, working_dir, connections, run_id, flow_id, line_number, variant_id)
        active_invoker = cls.active_instance()
        if active_invoker:
            active_invoker._deactivate_in_context()
        cls._activate_in_context(invoker)
        return invoker

    @classmethod
    def load_assistant_tools(cls, tools: list):
        invoker = cls.active_instance()
        for tool in tools:
            if tool["type"] != "promptflow_tool":
                continue
            inputs = tool.get("inputs", {})
            updated_inputs = {}
            for input_name, value in inputs.items():
                updated_inputs[input_name] = InputAssignment.deserialize(value)
            node = Node(
                name="assistant_node",
                tool="assistant_tool",
                inputs=updated_inputs,
                source=ToolSource.deserialize(tool["source"])
            )
            tool_resolver = ToolResolver(working_dir=invoker._working_dir, connections=invoker._connections)
            resolved_tool = tool_resolver._resolve_script_node(node, convert_input_types=True)
            if resolved_tool.node.inputs:
                inputs = {name: value.value for name, value in resolved_tool.node.inputs.items()}
                callable = partial(resolved_tool.callable, **inputs)
                resolved_tool.callable = callable
            invoker._assistant_tools[resolved_tool.definition.function] = resolved_tool
        return invoker

    def invoke_assistant_tool(self, func_name, kwargs):
        return self._assistant_tools[func_name].callable(**kwargs)

    def to_openai_tools(self):
        openai_tools = []
        for name, tool in self._assistant_tools.items():
            preset_inputs = [name for name, _ in tool.node.inputs.items()]
            description = self._get_openai_tool_description(name, tool.definition.description, preset_inputs)
            openai_tools.append(description)
        return openai_tools

    def _get_openai_tool_description(self, func_name: str, docstring: str, preset_inputs: Optional[list] = None):
        to_openai_type = {"str": "string", "int": "number"}

        doctree = publish_doctree(docstring)
        params = {}

        for field in doctree.traverse(docutils.nodes.field):
            field_name = field[0].astext()
            field_body = field[1].astext()

            if field_name.startswith("param"):
                param_name = field_name.split(' ')[1]
                if param_name in preset_inputs:
                    continue
                if param_name not in params:
                    params[param_name] = {}
                params[param_name]["description"] = field_body
            if field_name.startswith("type"):
                param_name = field_name.split(' ')[1]
                if param_name in preset_inputs:
                    continue
                if param_name not in params:
                    params[param_name] = {}
                params[param_name]["type"] = to_openai_type[field_body] if field_body in to_openai_type else field_body

        return {
            "type": "function",
            "function": {
                "name": func_name,
                "description": doctree[0].astext(),
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": list(params.keys())
                }
            }
        }

    def _update_operation_context(self):
        flow_context_info = {"flow-id": self._flow_id, "root-run-id": self._run_id}
        OperationContext.get_instance().update(flow_context_info)

    def invoke_tool(self, node: Node, f: Callable, kwargs):
        run_info = self._prepare_node_run(node, f, kwargs)
        node_run_id = run_info.run_id

        traces = []
        try:
            hit_cache = False
            # Get result from cache. If hit cache, no need to execute f.
            cache_info: CacheInfo = self._cache_manager.calculate_cache_info(self._flow_id, f, [], kwargs)
            if node.enable_cache and cache_info:
                cache_result: CacheResult = self._cache_manager.get_cache_result(cache_info)
                if cache_result and cache_result.hit_cache:
                    # Assign cached_flow_run_id and cached_run_id.
                    run_info.cached_flow_run_id = cache_result.cached_flow_run_id
                    run_info.cached_run_id = cache_result.cached_run_id
                    result = cache_result.result
                    hit_cache = True

            if not hit_cache:
                Tracer.start_tracing(node_run_id, node.name)
                result = self._invoke_tool_with_timer(node, f, kwargs)
                traces = Tracer.end_tracing(node_run_id)

            self._run_tracker.end_run(node_run_id, result=result, traces=traces)
            # Record result in cache so that future run might reuse its result.
            if not hit_cache and node.enable_cache:
                self._persist_cache(cache_info, run_info)

            flow_logger.info(f"Node {node.name} completes.")
            return result
        except Exception as e:
            logger.exception(f"Node {node.name} in line {self._line_number} failed. Exception: {e}.")
            if not traces:
                traces = Tracer.end_tracing(node_run_id)
            self._run_tracker.end_run(node_run_id, ex=e, traces=traces)
            raise
        finally:
            self._run_tracker.persist_node_run(run_info)

    def _prepare_node_run(self, node: Node, f, kwargs={}):
        # Ensure this thread has a valid operation context
        self._update_operation_context()
        node_run_id = self._generate_node_run_id(node)
        flow_logger.info(f"Executing node {node.name}. node run id: {node_run_id}")
        parent_run_id = f"{self._run_id}_{self._line_number}" if self._line_number is not None else self._run_id
        run_info: RunInfo = self._run_tracker.start_node_run(
            node=node.name,
            flow_run_id=self._run_id,
            parent_run_id=parent_run_id,
            run_id=node_run_id,
            index=self._line_number,
        )
        run_info.index = self._line_number
        run_info.variant_id = self._variant_id
        self._run_tracker.set_inputs(node_run_id, {key: value for key, value in kwargs.items() if key != "self"})
        return run_info

    async def invoke_tool_async(self, node: Node, f: Callable, kwargs):
        if not inspect.iscoroutinefunction(f):
            raise UnexpectedError(
                message_format="Tool '{function}' in node '{node}' is not a coroutine function.",
                function=f,
                node=node.name,
            )
        run_info = self._prepare_node_run(node, f, kwargs=kwargs)
        node_run_id = run_info.run_id

        traces = []
        try:
            Tracer.start_tracing(node_run_id, node.name)
            result = await self._invoke_tool_async_inner(node, f, kwargs)
            traces = Tracer.end_tracing(node_run_id)
            self._run_tracker.end_run(node_run_id, result=result, traces=traces)
            flow_logger.info(f"Node {node.name} completes.")
            return result
        except Exception as e:
            logger.exception(f"Node {node.name} in line {self._line_number} failed. Exception: {e}.")
            traces = Tracer.end_tracing(node_run_id)
            self._run_tracker.end_run(node_run_id, ex=e, traces=traces)
            raise
        finally:
            self._run_tracker.persist_node_run(run_info)

    async def _invoke_tool_async_inner(self, node: Node, f: Callable, kwargs):
        module = f.func.__module__ if isinstance(f, functools.partial) else f.__module__
        try:
            return await f(**kwargs)
        except PromptflowException as e:
            # All the exceptions from built-in tools are PromptflowException.
            # For these cases, raise the exception directly.
            if module is not None:
                e.module = module
            raise e
        except Exception as e:
            # Otherwise, we assume the error comes from user's tool.
            # For these cases, raise ToolExecutionError, which is classified as UserError
            # and shows stack trace in the error message to make it easy for user to troubleshoot.
            raise ToolExecutionError(node_name=node.name, module=module) from e

    def _invoke_tool_with_timer(self, node: Node, f: Callable, kwargs):
        module = f.func.__module__ if isinstance(f, functools.partial) else f.__module__
        node_name = node.name
        try:
            logging_name = node_name
            if self._line_number is not None:
                logging_name = f"{node_name} in line {self._line_number}"
            interval_seconds = 60
            start_time = time.perf_counter()
            thread_id = threading.current_thread().ident
            with RepeatLogTimer(
                interval_seconds=interval_seconds,
                logger=logger,
                level=WARNING,
                log_message_function=generate_elapsed_time_messages,
                args=(logging_name, start_time, interval_seconds, thread_id),
            ):
                return f(**kwargs)
        except PromptflowException as e:
            # All the exceptions from built-in tools are PromptflowException.
            # For these cases, raise the exception directly.
            if module is not None:
                e.module = module
            raise e
        except Exception as e:
            # Otherwise, we assume the error comes from user's tool.
            # For these cases, raise ToolExecutionError, which is classified as UserError
            # and shows stack trace in the error message to make it easy for user to troubleshoot.
            raise ToolExecutionError(node_name=node_name, module=module) from e

    def bypass_node(self, node: Node):
        """Update the bypassed node run info."""
        node_run_id = self._generate_node_run_id(node)
        flow_logger.info(f"Bypassing node {node.name}. node run id: {node_run_id}")
        parent_run_id = f"{self._run_id}_{self._line_number}" if self._line_number is not None else self._run_id
        run_info = self._run_tracker.bypass_node_run(
            node=node.name,
            flow_run_id=self._run_id,
            parent_run_id=parent_run_id,
            run_id=node_run_id,
            index=self._line_number,
            variant_id=self._variant_id,
        )
        self._run_tracker.persist_node_run(run_info)

    def _persist_cache(self, cache_info: CacheInfo, run_info: RunInfo):
        """Record result in cache storage if hash_id is valid."""
        if cache_info and cache_info.hash_id is not None and len(cache_info.hash_id) > 0:
            try:
                self._cache_manager.persist_result(run_info, cache_info, self._flow_id)
            except Exception as ex:
                # Not a critical path, swallow the exception.
                logging.warning(f"Failed to persist cache result. run_id: {run_info.run_id}. Exception: {ex}")

    def _generate_node_run_id(self, node: Node) -> str:
        if node.aggregation:
            # For reduce node, the id should be constructed by the flow run info run id
            return f"{self._run_id}_{node.name}_reduce"
        if self._line_number is None:
            return f"{self._run_id}_{node.name}_{uuid.uuid4()}"
        return f"{self._run_id}_{node.name}_{self._line_number}"
