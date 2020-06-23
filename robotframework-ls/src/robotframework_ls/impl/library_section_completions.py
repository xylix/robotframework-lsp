import os.path
from robocode_ls_core.robotframework_log import get_logger

log = get_logger(__name__)


def _create(library_name, selection, token, start_col_offset=None):
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


def add_completions_to_dir(curdir, matcher, ret, sel, token, value_to_cursor_split):
    for filename in os.listdir(curdir):
        check = None
        if filename.endswith(".py"):
            check = filename

        elif filename not in ("__pycache__", ".git") and os.path.isdir(
            os.path.join(curdir, filename)
        ):
            check = filename

        if check is not None:
            if matcher.accepts(check):
                ret.append(
                    _create(
                        check,
                        sel,
                        token,
                        start_col_offset=sel.col - len(value_to_cursor_split[1]),
                    )
                )


def complete(completion_context):
    from robotframework_ls.impl import ast_utils
    from robotframework_ls.impl.string_matcher import RobotStringMatcher
    from robocode_ls_core import uris
    from robotframework_ls.impl.robot_constants import BUILTIN_LIB

    ret = []

    token_info = completion_context.get_current_token()
    if token_info is not None:
        token = ast_utils.get_library_import_name_token(
            token_info.node, token_info.token
        )
        if token is not None:
            try:
                sel = completion_context.sel
                value_to_cursor = token.value
                if token.end_col_offset > sel.col:
                    value_to_cursor = value_to_cursor[
                        : -(token.end_col_offset - sel.col)
                    ]

                value_to_cursor_split = os.path.split(value_to_cursor)

                if os.path.isabs(token.value):
                    add_completions_to_dir(
                        value_to_cursor_split[0],
                        RobotStringMatcher(value_to_cursor_split[1]),
                        ret,
                        sel,
                        token,
                        value_to_cursor_split,
                    )

                else:
                    matcher = RobotStringMatcher(value_to_cursor)
                    libspec_manager = completion_context.workspace.libspec_manager
                    library_names = set(libspec_manager.get_library_names())
                    library_names.discard(BUILTIN_LIB)

                    for library_name in library_names:
                        if matcher.accepts(library_name):
                            ret.append(_create(library_name, sel, token))

                    # After checking the existing library names in memory (because we
                    # loaded them at least once), check libraries in the filesystem.
                    uri = completion_context.doc.uri
                    path = uris.to_fs_path(uri)
                    dirname = os.path.dirname(path)

                    matcher = RobotStringMatcher(value_to_cursor_split[1])
                    curdir = os.path.join(dirname, value_to_cursor_split[0])
                    add_completions_to_dir(
                        curdir, matcher, ret, sel, token, value_to_cursor_split
                    )
            except:
                log.exception()

    return ret
