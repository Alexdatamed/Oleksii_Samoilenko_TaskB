"""Gemini structured extraction and content-addressed, credential-free replay.

Live and replay return ExtractionResult(fields, issues, metadata). Only fields
and issues enter extraction/reconciliation logic; capture metadata is separate.
No fixture/reference data or parser answers are accessed by this module.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from app.schemas import ExtractionIssue, InvoiceFields

ROOT = Path(__file__).resolve().parents[1]
PROMPT_VERSION = 'invoice-gemini-v1'
SCHEMA_VERSION = 'invoice-fields-issues-v1'
GENERATION_SETTINGS = {'temperature': 0, 'max_output_tokens': 4096,
                       'candidate_count': 1, 'response_mime_type': 'application/json'}
TRANSIENT_CODES = {408, 429, 500, 502, 503, 504}
FIELD_NAMES = tuple(name for name in InvoiceFields.model_fields if name != 'file_id')


class ExtractionError(RuntimeError):
    """Sanitized processing failure, with portable metadata when available."""
    def __init__(self, message, metadata=None):
        super().__init__(message)
        self.metadata = metadata or {}


@dataclass
class ExtractionResult:
    fields: InvoiceFields
    issues: list[ExtractionIssue]
    metadata: dict


class _ResponseEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    fields: dict[str, Any]
    issues: list[ExtractionIssue]


def text_hash(text):
    return sha256(text.encode('utf-8')).hexdigest()


def json_hash(value):
    return text_hash(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False))


def _schema_subset(schema):
    """Strip defaults/titles/unsupported constraints; keep strict local validation."""
    supported = {'type', 'properties', 'required', 'items', 'enum', 'additionalProperties', 'description'}
    result = {k: v for k, v in schema.items() if k in supported}
    if 'anyOf' in schema:
        result['type'] = [alternative['type'] for alternative in schema['anyOf']]
    if 'properties' in result:
        result['properties'] = {k: _schema_subset(v) for k, v in result['properties'].items()}
    if 'items' in result:
        result['items'] = _schema_subset(result['items'])
    return result


def response_schema():
    shared = InvoiceFields.model_json_schema()['properties']
    fields = {name: _schema_subset(shared[name]) for name in FIELD_NAMES}
    issue = _schema_subset(ExtractionIssue.model_json_schema())
    issue['required'] = list(issue['properties'])
    return {'type': 'object', 'additionalProperties': False, 'required': ['fields', 'issues'],
            'properties': {'fields': {'type': 'object', 'additionalProperties': False,
                                      'properties': fields, 'required': list(fields)},
                           'issues': {'type': 'array', 'items': issue}}}


@dataclass(frozen=True)
class RequestConfiguration:
    model: str
    timeout_ms: int = 30000
    max_attempts: int = 3

    def __post_init__(self):
        if not re.fullmatch(r'(?:models/)?gemini-[A-Za-z0-9._-]+', self.model):
            raise ExtractionError('GEMINI_MODEL is missing or is not a valid Gemini model identifier')
        if self.timeout_ms <= 0 or not 1 <= self.max_attempts <= 3:
            raise ExtractionError('Invalid timeout or retry configuration')

    def bundle(self):
        prompt = (ROOT / 'prompts/extract_invoice.txt').read_text(encoding='utf-8-sig')
        schema = response_schema()
        configuration = {'provider': 'google-gemini', 'api': 'models.generate_content',
                         'requested_model': self.model, 'prompt_version': PROMPT_VERSION,
                         'schema_version': SCHEMA_VERSION, 'prompt_sha256': text_hash(prompt),
                         'schema_sha256': json_hash(schema), 'generation_settings': GENERATION_SETTINGS.copy(),
                         'transport': {'timeout_ms': self.timeout_ms, 'max_attempts': self.max_attempts,
                                       'sdk_attempts': 1, 'max_retry_wait_seconds': 30}}
        return prompt, schema, configuration


def load_live_configuration():
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env', override=False)
    key = os.environ.get('GEMINI_API_KEY', '')
    if not key.strip():
        raise ExtractionError('GEMINI_API_KEY is not configured')
    return RequestConfiguration(os.environ.get('GEMINI_MODEL', '')), key


def _response_text(sdk_response):
    candidates = sdk_response.get('candidates') or []
    if not candidates:
        return ''
    parts = (candidates[0].get('content') or {}).get('parts') or []
    return ''.join(part.get('text', '') for part in parts
                   if not part.get('thought') and isinstance(part.get('text'), str))


def _metadata(record, relative_name):
    sdk = record.get('sdk_response') or {}
    candidates = sdk.get('candidates') or []
    return {'capture': relative_name, 'origin': record['origin'],
            'ocr_sha256': record['ocr_sha256'], 'configuration_fingerprint': record['configuration_fingerprint'],
            'requested_model': record['configuration']['requested_model'],
            'returned_model': sdk.get('model_version'), 'sdk_version': record.get('sdk_version'),
            'usage': sdk.get('usage_metadata'),
            'finish_reason': candidates[0].get('finish_reason') if candidates else None,
            'attempts': record.get('attempts')}


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON keys')
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError('Nonfinite JSON numbers')


def process_response(raw_text, sdk_response, file_id):
    """Shared live/replay validation. Schema defects are processing errors."""
    feedback = sdk_response.get('prompt_feedback') or {}
    if feedback.get('block_reason') not in (None, 'BLOCK_REASON_UNSPECIFIED'):
        raise ExtractionError('Gemini response was blocked')
    candidates = sdk_response.get('candidates') or []
    if len(candidates) != 1:
        raise ExtractionError('Gemini response has no single completed candidate')
    finish = candidates[0].get('finish_reason')
    if finish != 'STOP':
        raise ExtractionError('Gemini response was truncated or did not finish normally')
    if not raw_text.strip():
        raise ExtractionError('Gemini returned empty response text')
    try:
        payload = json.loads(raw_text, object_pairs_hook=_unique_json, parse_constant=_reject_constant)
    except (ValueError, TypeError):
        raise ExtractionError('Gemini response is not valid, unambiguous JSON') from None
    try:
        envelope = _ResponseEnvelope.model_validate(payload)
        if set(envelope.fields) - set(FIELD_NAMES):
            raise ExtractionError('Gemini response contains unsupported model-generated fields')
        fields = InvoiceFields.model_validate({'file_id': file_id, **envelope.fields})
    except ValidationError:
        # Never stringify validation errors: they include untrusted input values.
        raise ExtractionError('Gemini response failed local Pydantic schema validation') from None
    issues = list(envelope.issues)
    for issue in issues:
        if getattr(fields, issue.field) is not None:
            raise ExtractionError('Gemini response has an issue for a non-null field')
    reported = {issue.field for issue in issues}
    for name in FIELD_NAMES:
        if getattr(fields, name) is None and name not in reported:
            issues.append(ExtractionIssue(field=name, code='missing',
                                          message='Model returned null or omitted the field'))
    return fields, issues


def _retry_delay(exc, attempt):
    """Honor Retry-After and google.rpc.RetryInfo; refuse excessive waits."""
    hints = []
    headers = getattr(getattr(exc, 'response', None), 'headers', {}) or {}
    value = headers.get('Retry-After')
    if value:
        try:
            hints.append(float(value))
        except ValueError:
            try:
                hints.append((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
    details = getattr(exc, 'details', {})
    if isinstance(details, dict):
        for detail in details.get('error', details).get('details', []):
            if isinstance(detail, dict) and detail.get('@type', '').endswith('RetryInfo'):
                delay = detail.get('retryDelay', '')
                if isinstance(delay, str) and re.fullmatch(r'\d+(?:\.\d+)?s', delay):
                    hints.append(float(delay[:-1]))
    return max([2 ** (attempt - 1), *hints])


def _safe_request_error(exc):
    code = getattr(exc, 'code', None)
    reasons = {400: 'invalid request or unsupported configuration', 401: 'credentials rejected',
               403: 'access denied', 404: 'configured model or endpoint unavailable',
               408: 'request timeout', 429: 'quota or rate limit exceeded'}
    if type(code) is int:
        reason = reasons.get(code, 'provider server error' if code >= 500 else 'provider request error')
        return f'Gemini request failed (HTTP {code}: {reason})'
    return 'Gemini transport or SDK request failed (details suppressed to protect credentials)'


def _write_record(directory, record, suffix):
    folder = Path(directory) / record['ocr_sha256']
    folder.mkdir(parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '_' + uuid4().hex + suffix
    path = folder / name
    with path.open('x', encoding='utf-8') as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
    return path.relative_to(directory).as_posix()


class GeminiExtractor:
    def __init__(self, capture_dir, configuration=None, api_key=None, *, origin='real_gemini'):
        if configuration is None:
            configuration, api_key = load_live_configuration()
        self.configuration = configuration
        self._api_key = api_key
        self.capture_dir = Path(capture_dir)
        self.origin = origin  # Tests explicitly use synthetic_test; CLI never does.
        self._client = None

    def _request(self, markdown, prompt, schema):
        from google import genai
        from google.genai import types
        import httpx
        if self._client is None:
            try:
                self._client = genai.Client(vertexai=False, api_key=self._api_key,
                                            http_options=types.HttpOptions(
                                                timeout=self.configuration.timeout_ms,
                                                retry_options=types.HttpRetryOptions(attempts=1)))
            except Exception:
                raise ExtractionError('Gemini SDK client initialization failed (details suppressed)') from None
        for attempt in range(1, self.configuration.max_attempts + 1):
            try:
                response = self._client.models.generate_content(
                    model=self.configuration.model, contents=markdown,
                    config=types.GenerateContentConfig(system_instruction=prompt,
                                                       response_json_schema=schema,
                                                       **GENERATION_SETTINGS))
                return response, attempt
            except Exception as exc:
                transient = getattr(exc, 'code', None) in TRANSIENT_CODES or isinstance(
                    exc, (httpx.TimeoutException, httpx.ConnectError))
                wait = _retry_delay(exc, attempt) if transient else 0
                if not transient or attempt == self.configuration.max_attempts or wait > 30:
                    raise ExtractionError(_safe_request_error(exc), {'attempts': attempt}) from None
                time.sleep(wait)

    def extract(self, markdown, file_id):
        prompt, schema, configuration = self.configuration.bundle()
        try:
            sdk_version = version('google-genai')
        except PackageNotFoundError:
            sdk_version = None
        record = {'capture_version': 1, 'origin': self.origin, 'timestamp': datetime.now(timezone.utc).isoformat(),
                  'ocr_text': markdown, 'ocr_sha256': text_hash(markdown), 'prompt': prompt, 'schema': schema,
                  'configuration': configuration, 'configuration_fingerprint': json_hash(configuration),
                  'sdk_version': sdk_version}
        if self._api_key and self._api_key in json.dumps(record, ensure_ascii=False):
            raise ExtractionError('Extraction input unexpectedly contains credential material')
        try:
            response, attempts = self._request(markdown, prompt, schema)
        except ExtractionError as exc:
            record.update(error=str(exc), attempts=exc.metadata.get('attempts'))
            relative_name = _write_record(self.capture_dir, record, '.failure.json')
            raise ExtractionError(str(exc), {'failure_record': relative_name, **exc.metadata,
                                            'requested_model': self.configuration.model}) from None
        # SDK HTTP headers are transport data and may be credential-bearing.
        sdk = response.model_dump(mode='json', exclude={'sdk_http_response'})
        raw_text = _response_text(sdk)
        record.update(sdk_response=sdk, original_response_text=raw_text,
                      response_sha256=text_hash(raw_text), sdk_response_sha256=json_hash(sdk), attempts=attempts)
        # Preserve original text; refuse to save it if credentials were echoed.
        if self._api_key and self._api_key in json.dumps(record, ensure_ascii=False):
            raise ExtractionError('Response unexpectedly contains credential material; capture suppressed')
        relative_name = _write_record(self.capture_dir, record, '.capture.json')
        metadata = _metadata(record, relative_name)
        try:
            fields, issues = process_response(raw_text, sdk, file_id)
        except ExtractionError as exc:
            raise ExtractionError(str(exc), metadata) from None
        return ExtractionResult(fields, issues, metadata)

    def close(self):
        if self._client is not None:
            self._client.close()


class ReplayExtractor:
    """Never loads .env, constructs an SDK client, or requires credentials."""
    def __init__(self, capture_dir, model=None, *, allow_synthetic=False):
        self.capture_dir = Path(capture_dir)
        self.model = model
        self.allow_synthetic = allow_synthetic

    def extract(self, markdown, file_id):
        ocr_hash = text_hash(markdown)
        paths = sorted((self.capture_dir / ocr_hash).glob('*.capture.json'), reverse=True)
        if not paths:
            raise ExtractionError('No captured Gemini response exists for this OCR content')
        for path in paths:
            try:
                record = json.loads(path.read_text(encoding='utf-8'))
                if record['capture_version'] != 1 or record['ocr_sha256'] != ocr_hash or text_hash(record['ocr_text']) != ocr_hash:
                    raise ExtractionError('Replay OCR hash or capture version mismatch')
                if record['origin'] != 'real_gemini' and not (self.allow_synthetic and record['origin'] == 'synthetic_test'):
                    raise ExtractionError('Replay requires a real Gemini capture; synthetic test data is not accepted')
                saved = record['configuration']
                if self.model is not None and saved['requested_model'] != self.model:
                    continue
                config = RequestConfiguration(saved['requested_model'])
                prompt, schema, current = config.bundle()
                if (record['configuration_fingerprint'] != json_hash(saved) or current != saved
                        or record['prompt'] != prompt or record['schema'] != schema):
                    continue
                sdk, raw = record['sdk_response'], record['original_response_text']
                if (record['response_sha256'] != text_hash(raw) or record['sdk_response_sha256'] != json_hash(sdk)
                        or raw != _response_text(sdk)):
                    raise ExtractionError('Replay response integrity mismatch')
            except (ValueError, KeyError, TypeError, OSError):
                raise ExtractionError('Replay capture is malformed or unreadable') from None
            relative_name = path.relative_to(self.capture_dir).as_posix()
            metadata = _metadata(record, relative_name)
            try:
                fields, issues = process_response(raw, sdk, file_id)
            except ExtractionError as exc:
                raise ExtractionError(str(exc), metadata) from None
            return ExtractionResult(fields, issues, metadata)
        raise ExtractionError('Replay extraction configuration fingerprint mismatch')
