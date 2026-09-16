"""Finite WeCom application HTTP transport for legacy business notifications.

Uses Hermes configuration and httpx; no gateway, scheduler, webhook or WS client.
Network methods are reachable only through the existing send activation gate.
Tokens stay in memory, and exceptions/receipts never include HTTP URLs or secrets.
"""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import hashlib
import io
import logging
from copy import deepcopy
import re
import time
import zipfile

from . import workflow_io as workflow

API = 'https://qyapi.weixin.qq.com/cgi-bin/'
MAX_FILE = 20 * 1024 * 1024


class _CredentialURLFilter(logging.Filter):
    def filter(self, record):
        # httpx INFO logs include query-string tokens. Keep unrelated requests,
        # but do not let this finite client's authentication URLs reach handlers.
        message=record.getMessage()
        return not any(value in message for value in ('qyapi.weixin.qq.com','access_token','corpsecret'))


_URL_FILTER=_CredentialURLFilter()


def load_apps():
    """Reuse the official loader, without starting an adapter or opening a socket."""
    from gateway.config import load_gateway_config
    config = load_gateway_config()
    matches = [value for key, value in config.platforms.items()
               if getattr(key, 'value', key) == 'wecom_callback']
    if len(matches) != 1:
        raise workflow.IOErrorBoundary('APPLICATION_CONFIGURATION_REQUIRED')
    extra = matches[0].extra
    apps = extra.get('apps')
    if isinstance(apps, list) and apps:
        return [dict(app) for app in apps if isinstance(app, dict)]
    return [{**extra, 'name': extra.get('name') or 'default'}] if extra.get('corp_id') else []


def file_snapshot(path, profile):
    path = Path(path)
    root = profile / 'report_runs' / 'legacy_execution'
    if not path.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in (path, *path.parents)):
        raise workflow.IOErrorBoundary('ATTACHMENT_PATH_INVALID')
    if not path.is_file() or not 5 <= path.stat().st_size <= MAX_FILE:
        raise workflow.IOErrorBoundary('ATTACHMENT_SIZE_INVALID')
    name = path.name
    if any(ord(c) < 32 or c in '"\\' for c in name):
        raise workflow.IOErrorBoundary('ATTACHMENT_NAME_INVALID')
    suffix = path.suffix.lower()
    types = {'.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
             '.zip': 'application/zip', '.png': 'image/png'}
    if suffix not in types:
        raise workflow.IOErrorBoundary('ATTACHMENT_TYPE_INVALID')
    data = path.read_bytes()
    if not 5 <= len(data) <= MAX_FILE:
        raise workflow.IOErrorBoundary('ATTACHMENT_SIZE_INVALID')
    digest = hashlib.sha256()
    digest.update(name.encode('utf-8'))
    if suffix in ('.xlsx', '.zip'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                names = [e.filename for e in entries]
                if (not entries or len(entries) > 10000 or len(set(names)) != len(names)
                        or sum(e.file_size for e in entries) > 100 * 1024 * 1024):
                    raise ValueError('archive bounds')
                if suffix == '.xlsx' and not {'[Content_Types].xml', 'xl/workbook.xml'} <= set(names):
                    raise ValueError('not xlsx')
                for entry in sorted(entries, key=lambda e: e.filename):
                    # Ignore ZIP timestamps/compression, which change on regeneration.
                    digest.update(entry.filename.encode('utf-8') + b'\0')
                    digest.update(hashlib.sha256(archive.read(entry)).digest())
        except Exception:
            raise workflow.IOErrorBoundary('ATTACHMENT_ARCHIVE_INVALID') from None
    else:
        if not data.startswith(b'\x89PNG\r\n\x1a\n'):
            raise workflow.IOErrorBoundary('ATTACHMENT_PNG_INVALID')
        digest.update(data)
    return name, data, types[suffix], digest.hexdigest()


class AppTransport:
    def __init__(self, job, profile, *, app_loader=load_apps, client_factory=None):
        self.job, self.profile = job, Path(profile)
        self.target_map = deepcopy(self._binding().get('target_map') or {})
        self.app_loader, self.client_factory = app_loader, client_factory
        self.targets, self.apps, self.files, self.media = {}, {}, {}, {}
        self.tokens = {}
        self.client = None

    def _binding(self):
        return workflow.require_action(self.job,'send_enabled')

    def _gate(self):
        if self._binding().get('target_map') != self.target_map:
            raise workflow.IOErrorBoundary('DELIVERY_CONFIGURATION_CHANGED')

    def normalize(self, components, progress):
        result=[]
        for item in components:
            if item['kind']!='text' or len(item['text'].encode('utf-8'))<=2048:
                result.append(item);continue
            if progress.status(item['key'])!='not_attempted':
                raise workflow.IOErrorBoundary('LEGACY_TEXT_PROGRESS_REQUIRES_REVIEW')
            chunks=[];chunk='';size=0
            for char in item['text']:
                width=len(char.encode('utf-8'))
                if size+width>2048:chunks.append(chunk);chunk='';size=0
                chunk+=char;size+=width
            if chunk:chunks.append(chunk)
            result.extend({**item,'text':chunk,'key':workflow.wf.digest([item['key'],'utf8-part',index]),
                           'notification_key':item.get('notification_key') or workflow.wf.digest([item['account']])}
                          for index,chunk in enumerate(chunks))
        return result

    def preflight(self, components):
        self._gate()
        self.targets={};self.files={}
        if len(components)>2000:raise workflow.IOErrorBoundary('NOTIFICATION_BATCH_TOO_LARGE')
        configured = self.app_loader()
        for item in components:
            target = self.target_map.get(item['account'])
            required = {'platform', 'app_name', 'corp_id', 'agent_id', 'target_kind', 'target_id'}
            if (not isinstance(target, dict) or set(target) != required
                    or any(not isinstance(target[k],str) or not target[k] for k in required)
                    or target.get('platform') != 'wecom_app_http'
                    or target.get('target_kind') not in ('user', 'appchat')
                    or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,128}', str(target.get('target_id', '')))
                    or str(target.get('target_id')).lower() == '@all'):
                raise workflow.IOErrorBoundary('EXPLICIT_APPLICATION_TARGET_REQUIRED')
            matches = [app for app in configured if app.get('name') == target['app_name']
                       and str(app.get('corp_id')) == str(target['corp_id'])
                       and str(app.get('agent_id')) == str(target['agent_id'])]
            if len(matches) != 1 or not matches[0].get('corp_secret') or not str(target['agent_id']).isdigit():
                raise workflow.IOErrorBoundary('EXACT_APPLICATION_CREDENTIALS_REQUIRED')
            app_key = (str(target['corp_id']), str(target['agent_id']))
            if app_key in self.apps and self.apps[app_key] != matches[0]:
                raise workflow.IOErrorBoundary('APPLICATION_ALIAS_CONFLICT')
            self.apps[app_key] = matches[0]
            self.targets[item['account']] = dict(target)
            if item['kind'] == 'text':
                if len(item.get('text', '').encode('utf-8')) > 2048:
                    raise workflow.IOErrorBoundary('APPLICATION_TEXT_BYTE_LIMIT_EXCEEDED')
            elif item['kind'] == 'file':
                # Coarse preflight before business reads/freezing has no path yet.
                if 'path' in item:
                    self.files[item['key']] = file_snapshot(item['path'], self.profile)
            else:
                raise workflow.IOErrorBoundary('APPLICATION_COMPONENT_INVALID')

    def target_key(self, account):
        target = self.targets[account]
        identity=target['target_id'].lower() if target['target_kind']=='user' else target['target_id']
        return workflow.wf.digest([str(target['corp_id']),str(target['agent_id']),target['target_kind'],identity])

    @contextmanager
    def delivery_lock(self, progress):
        # Shared across jobs and account aliases. Contention fails closed; no queue.
        with ExitStack() as stack:
            for key in sorted({self.target_key(account) for account in self.targets}):
                stack.enter_context(workflow.run_lock(self.profile, 'notification-target-' + key))
            try:
                yield
            finally:
                if self.client is not None:
                    self.client.close()
                    self.client = None
                self.files.clear()
                self.media.clear()

    def fingerprint(self, item):
        content = self.files[item['key']][3] if item['kind'] == 'file' else item['text']
        return workflow.wf.digest([self.target_key(item['account']), item['kind'], content])

    def _fence_path(self, item):
        return workflow.private_root(self.profile)/('notification-target-'+self.target_key(item['account'])+'.json')

    def _owner(self, item, progress):
        return workflow.wf.digest([progress.scope,item['notification_key']])

    def _check_fence(self, item, progress):
        import json
        path=self._fence_path(item)
        if path.is_symlink():raise workflow.IOErrorBoundary('WORKFLOW_STATE_PATH_INVALID')
        if path.exists():
            value=json.loads(path.read_text(encoding='utf-8'))
            if value.get('status') in ('in_flight','unknown','unverified_success','not_delivered'):
                raise workflow.IOErrorBoundary('TARGET_DELIVERY_UNKNOWN_REVIEW_REQUIRED')
            if value.get('status')!='failed' or value.get('owner')!=self._owner(item,progress):
                raise workflow.IOErrorBoundary('TARGET_NOTIFICATION_PENDING_REVIEW_REQUIRED')

    def begin_notification(self, items, progress):
        item=items[0];self._check_fence(item,progress)
        workflow.operations._atomic(self._fence_path(item),{'owner':self._owner(item,progress),'status':'in_flight'})

    def finish_notification(self, items, progress, status):
        item=items[0];path=self._fence_path(item)
        if status=='provider_accepted':path.unlink()
        else:workflow.operations._atomic(path,{'owner':self._owner(item,progress),'status':status})

    def _request(self, method, endpoint, **kwargs):
        self._gate()
        if endpoint not in {'gettoken', 'user/get', 'appchat/get', 'media/upload', 'message/send', 'appchat/send','webhook/upload_media','webhook/send'}:
            raise workflow.IOErrorBoundary('APPLICATION_ENDPOINT_INVALID')
        if self.client is None:
            if self.client_factory is None:
                import httpx
                # No redirects/retries; retain normal verified TLS/proxy policy.
                self.client = httpx.Client(timeout=45, follow_redirects=False)
            else:
                self.client = self.client_factory()
        # Filters are attached to producing loggers, not just their parents.
        # They redact URL-bearing records from this host; no logging level or
        # gateway setting is changed, and HTTP request/response bodies aren't logged.
        for name in ('httpx','httpcore.connection','httpcore.http11','httpcore.http2','httpcore.proxy'):
            logger=logging.getLogger(name)
            if _URL_FILTER not in logger.filters:logger.addFilter(_URL_FILTER)
        try:
            response = self.client.request(method, API + endpoint, **kwargs)
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError('invalid response')
            return value
        except Exception:
            # Never leak token-bearing URLs or provider error text into audit/logs.
            raise workflow.IOErrorBoundary('APPLICATION_HTTP_OUTCOME_UNKNOWN') from None

    def _token(self, target):
        key = (str(target['corp_id']), str(target['agent_id']))
        token, expiry = self.tokens.get(key, (None, 0))
        if token and expiry > time.monotonic() + 60:
            return token
        app = self.apps[key]
        value = self._request('GET', 'gettoken', params={'corpid': app['corp_id'], 'corpsecret': app['corp_secret']})
        if value.get('errcode') != 0 or not isinstance(value.get('access_token'), str) or not value['access_token']:
            raise workflow.IOErrorBoundary('APPLICATION_TOKEN_REJECTED')
        self.tokens[key] = value['access_token'], time.monotonic() + int(value.get('expires_in', 7200))
        return value['access_token']

    def prepare(self, items, progress):
        """Validate each distinct target, then upload ALL pending files before text."""
        first_targets=set()
        for item in items:
            target_key=self.target_key(item['account'])
            if target_key not in first_targets:self._check_fence(item,progress)
            first_targets.add(target_key)
        checked = set()
        for item in items:
            target = self.targets[item['account']]
            key = self.target_key(item['account'])
            if key in checked:
                continue
            token = self._token(target)
            group = target['target_kind'] == 'appchat'
            response = self._request('GET', 'appchat/get' if group else 'user/get',
                params={'access_token': token, 'chatid' if group else 'userid': target['target_id']})
            valid = (response.get('chat_info', {}).get('chatid') == target['target_id'] if group else
                     str(response.get('userid', '')).lower() == target['target_id'].lower() and response.get('status') == 1)
            if response.get('errcode') != 0 or not valid:
                raise workflow.IOErrorBoundary('APPLICATION_TARGET_NOT_VERIFIED')
            checked.add(key)
        for item in items:
            if item['kind'] != 'file':
                continue
            target = self.targets[item['account']]
            name, data, content_type, digest = self.files[item['key']]
            # The bytes checked above are exactly the bytes uploaded, even if the
            # generator changes the original file between validation and upload.
            upload_key = 'upload-' + item['key']
            progress.set(upload_key, 'upload_in_flight', artifact_digest=digest)
            try:
                value = self._request('POST', 'media/upload',
                    params={'access_token': self._token(target), 'type': 'file'},
                    files={'media': (name, data, content_type)})
            except workflow.IOErrorBoundary:
                progress.set(upload_key, 'upload_unknown', artifact_digest=digest)
                raise
            if value.get('errcode', 0) != 0 or not isinstance(value.get('media_id'), str) or not value['media_id']:
                progress.set(upload_key, 'upload_failed', api_errcode=value.get('errcode'), artifact_digest=digest)
                raise workflow.IOErrorBoundary('APPLICATION_UPLOAD_FAILED')
            self.media[item['key']] = value['media_id']
            progress.set(upload_key, 'uploaded_not_sent', media_id=value['media_id'], artifact_digest=digest)

    def send(self, item):
        self._gate()
        target = self.targets[item['account']]
        group = target['target_kind'] == 'appchat'
        payload = {'msgtype': item['kind'], 'safe': 0}
        if group:
            payload['chatid'] = target['target_id']
        else:
            payload.update(touser=target['target_id'], agentid=int(target['agent_id']))
        if item['kind'] == 'text':
            payload['text'] = {'content': item['text']}
        else:
            payload['file'] = {'media_id': self.media[item['key']]}
        value = self._request('POST', 'appchat/send' if group else 'message/send',
            params={'access_token': self._token(target)}, json=payload)
        code = value.get('errcode')
        # A zero code with invalid recipients is not successful delivery. There is
        # exactly one user here; no partial multi-recipient send can be hidden.
        invalid = any(value.get(k) for k in ('invaliduser', 'invalidparty', 'invalidtag', 'unlicenseduser'))
        return {'success': type(code) is int and code == 0 and not invalid,
                'message_id': value.get('msgid'),
                'raw_response': {'errcode': code, 'invalid_recipient': invalid}}
