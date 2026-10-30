"""Offline document storage, Responses mapping, and download boundary checks."""
import base64
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch
import zipfile

import web_app
from chat_files import FILE_TYPES, MAX_FILE_BYTES, decode_document, save_document
from chat_session import Message
from openai_chat import OpenAIChatTransport
from test_openai_chat import response


def office_file(member):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr(member, '<root/>')
    return stream.getvalue()


def test_uploads_and_history():
    fixtures = {'.md': '# 测试\n金额 42'.encode(), '.txt': b'hello', '.csv': b'name,value\na,42',
                '.pdf': b'%PDF-1.4\nfixture', '.docx': office_file('word/document.xml'),
                '.xlsx': office_file('xl/workbook.xml')}
    with tempfile.TemporaryDirectory() as directory, patch('web_app.CHAT_ATTACHMENT_DIR', Path(directory)):
        raw = [{'name': '测试' + suffix, 'dataUrl': 'data:application/octet-stream;base64,' + base64.b64encode(data).decode()}
               for suffix, data in fixtures.items()]
        files = web_app.process_chat_attachments(raw, '')
        assert len(files) == 6 and all(f['kind'] == 'file' for f in files)
        assert all(web_app.chat_attachment_path(f['id']).read_bytes() == data for f, data in zip(files, fixtures.values()))
        messages = [Message(id='u', role='user', content='分析附件', attachments=files),
                    Message(id='next', role='user', content='继续')]
        history, _ = web_app.build_chat_history_context(messages, 'pending', include_files=True)
        parts = OpenAIChatTransport().input_items(history)[0]['content']
        assert len([p for p in parts if p['type'] == 'input_file']) == 6
        assert all(p['file_data'].startswith('data:') for p in parts if p['type'] == 'input_file')
        assert web_app.chat_attachment_path('../escape') is None
        for name, data in [('fake.docx', b'not zip'), ('script.exe', b'MZ'), ('bad.pdf', b'text'), ('large.txt', b'a' * (MAX_FILE_BYTES + 1))]:
            try:
                save_document(Path(directory), name, data)
                raise AssertionError('invalid document accepted')
            except ValueError:
                pass
        handler = Mock()
        handler.wfile = io.BytesIO()
        web_app.Handler.serve_chat_attachment(handler, files[0]['id'])
        assert handler.wfile.getvalue() == fixtures['.md']
        headers = dict(call.args for call in handler.send_header.call_args_list)
        assert headers['Content-Disposition'].startswith("attachment; filename*=UTF-8''")
        assert headers['X-Content-Type-Options'] == 'nosniff'


def test_generated_files_and_helper_boundary():
    saved = Mock(return_value='/api/chat/attachments/local-file')
    annotation = {'type': 'container_file_citation', 'container_id': 'cntr_test',
                  'file_id': 'cfile_test', 'filename': '报告.md'}
    result = response([{'type': 'message', 'content': [{'type': 'output_text',
        'text': '[报告](sandbox:/mnt/data/report.md)', 'annotations': [annotation]}]}])
    download = Mock(status_code=200)
    download.__enter__ = Mock(return_value=download)
    download.__exit__ = Mock(return_value=False)
    download.iter_content.return_value = ['# 报告'.encode()]
    transport = OpenAIChatTransport(on_file=saved)
    with patch('openai_chat.requests.Session.post', return_value=result) as post, patch('openai_chat.requests.Session.get', return_value=download) as get:
        body = transport.post('ignored', json={'messages': [], 'enable_file_tools': True}).json()
        assert post.call_args.kwargs['json']['tools'][0]['type'] == 'code_interpreter'
        assert 'sandbox:' not in body['choices'][0]['message']['content']
        assert '/api/chat/attachments/local-file' in body['choices'][0]['message']['content']
        saved.assert_called_once_with('报告.md', '# 报告'.encode())
        assert get.call_args.args[0] == 'https://api.openai.com/v1/containers/cntr_test/files/cfile_test/content'
        assert get.call_args.kwargs['allow_redirects'] is False
        transport.download_generated_file(annotation)
        assert get.call_count == 1
        transport.post('ignored', json={'messages': []})
        assert 'tools' not in post.call_args.kwargs['json']
        try:
            transport.download_generated_file({**annotation, 'container_id': '../other'})
            raise AssertionError('unsafe path accepted')
        except RuntimeError:
            pass


if __name__ == '__main__':
    test_uploads_and_history()
    test_generated_files_and_helper_boundary()
    print('chat file tests passed')
