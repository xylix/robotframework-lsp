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
                if os.path.isabs(token.value):
                    pass

                else:
                    sel = completion_context.sel
                    filter_text = token.value
                    if token.end_col_offset > sel.col:
                        filter_text = filter_text[: -(token.end_col_offset - sel.col)]
                    matcher = RobotStringMatcher(filter_text)
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
                    split = os.path.split(filter_text)

                    matcher = RobotStringMatcher(split[1])
                    curdir = os.path.join(dirname, split[0])
                    for filename in os.listdir(curdir):
                        check = None
                        if filename.endswith(".py"):
                            filename = os.path.splitext(filename)[0]
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
                                        start_col_offset=sel.col - len(split[1]),
                                    )
                                )
            except:
                log.exception()

    return ret
