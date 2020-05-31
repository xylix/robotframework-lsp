"""
Unfortunately right now Robotframework doesn't really provide the needed hooks
for a debugger, so, we monkey-patch internal APIs to gather the needed info.

More specifically:
 
    robot.running.steprunner.StepRunner - def run_step
    
    is patched so that we can stop when some line is about to be executed.
"""
import functools
from robotframework_debug_adapter import file_utils
import threading
from robotframework_debug_adapter.constants import (
    STATE_RUNNING,
    STATE_PAUSED,
    REASON_BREAKPOINT,
    STEP_IN,
    REASON_STEP,
    STEP_NEXT,
)
import itertools
from functools import partial
from os.path import os
from robocode_ls_core.robotframework_log import get_logger
from collections import namedtuple
from robocode_ls_core.constants import IS_PY2
import weakref


log = get_logger(__name__)

next_id = partial(next, itertools.count(1))


class RobotBreakpoint(object):
    def __init__(self, lineno):
        """
        :param int lineno:
            1-based line for the breakpoint.
        """
        self.lineno = lineno


class BusyWait(object):
    def __init__(self):
        self._event = threading.Event()
        self.before_wait = []

    def wait(self):
        for c in self.before_wait:
            c()
        self._event.wait()

    def proceed(self):
        self._event.set()
        self._event.clear()


class _IterableToDAP(object):
    def compute_as_dap(self):
        return []


class _ArgsAsDAP(_IterableToDAP):
    def __init__(self, keyword_args):
        self._keyword_args = keyword_args

    def compute_as_dap(self):
        from robotframework_debug_adapter.dap.dap_schema import Variable
        from robotframework_debug_adapter.safe_repr import SafeRepr

        lst = []
        safe_repr = SafeRepr()
        for i, arg in enumerate(self._keyword_args):
            lst.append(
                Variable("param %s" % (i,), safe_repr(arg), variablesReference=0)
            )
        return lst


class _VariablesAsDAP(_IterableToDAP):
    def __init__(self, ctx):
        self._ctx = ctx

    def compute_as_dap(self):
        from robotframework_debug_adapter.dap.dap_schema import Variable
        from robotframework_debug_adapter.safe_repr import SafeRepr

        variables = self._ctx.namespace.variables
        as_dct = variables.as_dict()
        lst = []
        safe_repr = SafeRepr()
        for key, val in as_dct.items():
            lst.append(Variable(safe_repr(key), safe_repr(val), variablesReference=0))
        return lst


class _FrameInfo(object):
    def __init__(self, stack_list, dap_frame, keyword, ctx):
        self._stack_list = weakref.ref(stack_list)
        self._dap_frame = dap_frame
        self._keyword = keyword
        self._scopes = None
        self._ctx = ctx

    @property
    def dap_frame(self):
        return self._dap_frame

    def get_scopes(self):
        if self._scopes is not None:
            return self._scopes
        stack_list = self._stack_list()
        if stack_list is None:
            return []

        from robotframework_debug_adapter.dap.dap_schema import Scope

        locals_variables_reference = next_id()
        vars_variables_reference = next_id()
        scopes = [
            Scope("Variables", vars_variables_reference, expensive=False),
            Scope(
                "Arguments",
                locals_variables_reference,
                expensive=False,
                presentationHint="locals",
            ),
        ]

        try:
            args = self._keyword.args
        except:
            log.debug("Unable to get arguments for keyword: %s", self._keyword)
            args = []
        stack_list.register_variables_reference(
            locals_variables_reference, _ArgsAsDAP(args)
        )
        #             ctx.namespace.get_library_instances()
        #             keyword.args

        stack_list.register_variables_reference(
            vars_variables_reference, _VariablesAsDAP(self._ctx)
        )
        self._scopes = scopes
        return self._scopes


class _StackList(object):
    def __init__(self):
        self._frame_id_to_frame_info = {}
        self._dap_frames = []
        self._ref_id_to_children = {}

    def iter_frame_ids(self):
        return iter(self._frame_id_to_frame_info.keys())

    def register_variables_reference(self, variables_reference, children):
        self._ref_id_to_children[variables_reference] = children

    def add_stack(self, keyword, name, filename, ctx):
        from robotframework_debug_adapter.dap import dap_schema

        frame_id = next_id()
        dap_frame = dap_schema.StackFrame(
            frame_id,
            name=str(keyword),
            line=keyword.lineno,
            column=0,
            source=dap_schema.Source(name=name, path=filename),
        )
        self._dap_frames.append(dap_frame)
        self._frame_id_to_frame_info[frame_id] = _FrameInfo(
            self, dap_frame, keyword, ctx
        )
        return frame_id

    @property
    def dap_frames(self):
        return self._dap_frames

    def get_scopes(self, frame_id):
        frame_info = self._frame_id_to_frame_info.get(frame_id)
        if frame_info is None:
            return None
        return frame_info.get_scopes()

    def get_variables(self, variables_reference):
        lst = self._ref_id_to_children.get(variables_reference)
        if lst is not None:
            if isinstance(lst, _IterableToDAP):
                lst = lst.compute_as_dap()
        return lst


_StepEntry = namedtuple("_StepEntry", "ctx, step")


class _RobotDebuggerImpl(object):
    def __init__(self):
        from collections import deque

        self._filename_to_line_to_breakpoint = {}
        self.busy_wait = BusyWait()

        self._run_state = STATE_RUNNING
        self._step_cmd = None
        self._reason = None
        self._next_id = next_id
        self._ctx_deque = deque()
        self._step_next_stack_len = 0

        self._tid_to_stack_list = {}
        self._frame_id_to_tid = {}

    @property
    def stop_reason(self):
        return self._reason

    def get_stack_list(self, thread_id):
        return self._tid_to_stack_list.get(thread_id)

    def get_frames(self, thread_id):
        stack_list = self.get_stack_list(thread_id)
        return stack_list.dap_frames

    def get_scopes(self, frame_id):
        tid = self._frame_id_to_tid.get(frame_id)
        if tid is None:
            return None

        stack_list = self.get_stack_list(tid)
        return stack_list.get_scopes(frame_id)

    def get_variables(self, variables_reference):
        for stack_list in list(self._tid_to_stack_list.values()):
            variables = stack_list.get_variables(variables_reference)
            if variables is not None:
                return variables

    def _create_stack_list(self, thread_id):

        stack_list = _StackList()
        for step_entry in reversed(self._ctx_deque):
            keyword = step_entry.step
            ctx = step_entry.ctx
            filename, _changed = file_utils.norm_file_to_client(keyword.source)
            name = os.path.basename(filename)
            frame_id = stack_list.add_stack(keyword, name, filename, ctx)

        for frame_id in stack_list.iter_frame_ids():
            self._frame_id_to_tid[frame_id] = thread_id

        self._tid_to_stack_list[thread_id] = stack_list

    def _dispose_stack_list(self, thread_id):
        stack_list = self._tid_to_stack_list.pop(thread_id)
        for frame_id in stack_list.iter_frame_ids():
            self._frame_id_to_tid.pop(frame_id)

    def wait_suspended(self, reason):
        from robotframework_debug_adapter.constants import MAIN_THREAD_ID

        log.info("wait_suspended", reason)
        self._create_stack_list(MAIN_THREAD_ID)
        try:
            self._run_state = STATE_PAUSED
            self._reason = reason

            while self._run_state == STATE_PAUSED:
                self.busy_wait.wait()

            if self._step_cmd == STEP_NEXT:
                self._step_next_stack_len = len(self._ctx_deque)

        finally:
            self._dispose_stack_list(MAIN_THREAD_ID)

    def step_continue(self):
        self._step_cmd = None
        self._run_state = STATE_RUNNING
        self.busy_wait.proceed()

    def step_in(self):
        self._step_cmd = STEP_IN
        self._run_state = STATE_RUNNING
        self.busy_wait.proceed()

    def step_next(self):
        self._step_cmd = STEP_NEXT
        self._run_state = STATE_RUNNING
        self.busy_wait.proceed()

    def set_breakpoints(self, filename, breakpoints):
        filename = file_utils.get_abs_path_real_path_and_base_from_file(filename)[0]
        line_to_bp = {}
        for bp in breakpoints:
            line_to_bp[bp.lineno] = bp
        self._filename_to_line_to_breakpoint[filename] = line_to_bp

    def before_run_step(self, step_runner, step):
        ctx = step_runner._context
        self._ctx_deque.append(_StepEntry(ctx, step))

        try:
            lineno = step.lineno
            source = step.source
            if IS_PY2 and isinstance(source, unicode):
                source = source.encode(file_utils.file_system_encoding)
        except AttributeError:
            return

        log.debug("run_step %s, %s - step: %s\n", step, lineno, self._step_cmd)
        source = file_utils.get_abs_path_real_path_and_base_from_file(source)[0]
        lines = self._filename_to_line_to_breakpoint.get(source)

        stop_reason = None
        step_cmd = self._step_cmd
        if lines and step.lineno in lines:
            stop_reason = REASON_BREAKPOINT

        elif step_cmd is not None:
            if step_cmd == STEP_IN:
                stop_reason = REASON_STEP

            elif step_cmd == STEP_NEXT:
                if len(self._ctx_deque) <= self._step_next_stack_len:
                    stop_reason = REASON_STEP

        if stop_reason is not None:
            ctx = step_runner._context
            self.wait_suspended(stop_reason)

    def after_run_step(self, step_runner, keyword):
        self._ctx_deque.pop()


def _patch(
    execution_context_cls, impl, method_name, call_before_method, call_after_method
):

    original_method = getattr(execution_context_cls, method_name)

    @functools.wraps(original_method)
    def new_method(*args, **kwargs):
        call_before_method(*args, **kwargs)
        try:
            ret = original_method(*args, **kwargs)
        finally:
            call_after_method(*args, **kwargs)
        return ret

    setattr(execution_context_cls, method_name, new_method)


def patch_execution_context():
    from robot.running.steprunner import StepRunner

    try:
        impl = patch_execution_context.impl
    except AttributeError:
        # Note: only patches once, afterwards, returns the same instance.

        impl = _RobotDebuggerImpl()
        _patch(StepRunner, impl, "run_step", impl.before_run_step, impl.after_run_step)
        patch_execution_context.impl = impl

    return patch_execution_context.impl
