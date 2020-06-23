import os.path
from robocode_ls_core.robotframework_log import get_logger

log = get_logger(__name__)


def _create_completion_item(library_name, selection, token, start_col_offset=None):
    from robocode_ls_core.lsp import (
        CompletionItem,
        InsertTextFormat,
        Position,
        Range,
        TextEdit,
    )
    from robocode_ls_core.lsp import MarkupKind
    from robocode_ls_core.lsp import CompletionItemKind

    text_edit = TextEdit(
        Range(
            start=Position(
                selection.line,
                start_col_offset if start_col_offset is not None else token.col_offset,
            ),
            end=Position(selection.line, token.end_col_offset),
        ),
        library_name,
    )

    # text_edit = None
    return CompletionItem(
        library_name,
        kind=CompletionItemKind.Module,
        text_edit=text_edit,
        documentation="",
        insertTextFormat=InsertTextFormat.Snippet,
        documentationFormat=MarkupKind.PlainText,
    ).to_dict()


def add_completions_from_dir(directory, matcher, ret, sel, token, qualifier):
    for filename in os.listdir(directory):
        check = None
        if filename.endswith(".py"):
            check = filename

        elif filename not in ("__pycache__", ".git") and os.path.isdir(
            os.path.join(directory, filename)
        ):
            check = filename + "/"

        if check is not None:
            if matcher.accepts(check):
                ret.append(
                    _create_completion_item(
                        check, sel, token, start_col_offset=sel.col - len(qualifier)
                    )
                )


def _get_resource_completions(completion_context, token):
    return []


def _get_library_completions(completion_context, token):
    from robotframework_ls.impl.string_matcher import RobotStringMatcher
    from robocode_ls_core import uris
    from robotframework_ls.impl.robot_constants import BUILTIN_LIB

    ret = []

    sel = completion_context.sel
    value_to_cursor = token.value
    if token.end_col_offset > sel.col:
        value_to_cursor = value_to_cursor[: -(token.end_col_offset - sel.col)]

    value_to_cursor_split = os.path.split(value_to_cursor)

    if os.path.isabs(token.value):
        add_completions_from_dir(
            value_to_cursor_split[0],
            RobotStringMatcher(value_to_cursor_split[1]),
            ret,
            sel,
            token,
            value_to_cursor_split[1],
        )

    else:
        matcher = RobotStringMatcher(value_to_cursor)
        libspec_manager = completion_context.workspace.libspec_manager
        library_names = set(libspec_manager.get_library_names())
        library_names.discard(BUILTIN_LIB)

        for library_name in library_names:
            if matcher.accepts(library_name):
                ret.append(_create_completion_item(library_name, sel, token))

        # After checking the existing library names in memory (because we
        # loaded them at least once), check libraries in the filesystem.
        uri = completion_context.doc.uri
        path = uris.to_fs_path(uri)
        dirname = os.path.dirname(path)

        matcher = RobotStringMatcher(value_to_cursor_split[1])
        directory = os.path.join(dirname, value_to_cursor_split[0])
        add_completions_from_dir(
            directory, matcher, ret, sel, token, value_to_cursor_split[1]
        )
    return ret


def complete(completion_context):
    from robotframework_ls.impl import ast_utils

    ret = []

    try:
        token_info = completion_context.get_current_token()
        if token_info is not None:
            token = ast_utils.get_library_import_name_token(
                token_info.node, token_info.token
            )
            if token is not None:
                ret = _get_library_completions(completion_context, token)
            else:
                token = ast_utils.get_resource_import_name_token(
                    token_info.node, token_info.token
                )
                ret = _get_resource_completions(completion_context, token)

    except:
        log.exception()

    return ret
