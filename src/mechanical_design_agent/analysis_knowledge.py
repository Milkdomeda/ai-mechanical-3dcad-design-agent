"""Read-only analysis knowledge seam; never records into CAD session state."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json

from .analysis_protocol import _check_json, canonical_record, parse_record
from .analysis_types import AnalysisError
from .design_knowledge import _knowledge_ids
from .models import require_safe_id


def _json_bytes(value):
    _check_json(value)
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(encoded) > 4 * 1024 * 1024:
        raise AnalysisError('ANALYSIS_KNOWLEDGE_LIMIT', 'knowledge context exceeds the supported size')
    return encoded


class AnalysisKnowledgeService:
    def __init__(self, runtime, context_loader):
        self.runtime = runtime
        # Same approved context loader as CAD, without its record_knowledge call.
        self.context_loader = context_loader

    def retrieve(self, *, design_id, analysis_id, source_sha256, query, features, required_ids, requested_family_id=None):
        require_safe_id(design_id, 'design_id')
        require_safe_id(analysis_id, 'analysis_id')
        if type(query) is not str or not query.strip() or len(query) > 4096 or type(features) is not dict or type(required_ids) is not list or len(required_ids) > 5000 or any(type(value) is not str or not value.strip() or len(value) > 512 for value in required_ids):
            raise AnalysisError('ANALYSIS_KNOWLEDGE_REQUEST_INVALID', 'a bounded query, feature object and explicit required knowledge identifiers are required')
        if set(features) & {'organization_id', 'design_group_id', 'actor_id', 'scope', 'requested_family_id'}:
            raise AnalysisError('ANALYSIS_KNOWLEDGE_SCOPE_INVALID', 'knowledge authority comes from the workspace; family selection has its own explicit field')
        if requested_family_id is not None:
            require_safe_id(requested_family_id, 'requested_family_id')
        manifest = self.runtime._manifest()
        scope = {**self.runtime.design_knowledge_scope(), 'requested_family_id': requested_family_id}
        feature_bytes = _json_bytes(features)
        selected_features = json.loads(feature_bytes)
        if requested_family_id is not None:
            selected_features['requested_family_id'] = requested_family_id
        required = sorted(set(required_ids))
        context = None
        context_sha = None
        warning = None
        available = []
        try:
            loaded = self.context_loader(query.strip(), selected_features)
            if type(loaded) is not dict or loaded.get('schema_version') != 'DesignContext/v2':
                raise ValueError('unsupported knowledge context')
            context_bytes = _json_bytes(loaded)
            context = json.loads(context_bytes)
            available = sorted(set(_knowledge_ids(context)))
            context_sha = hashlib.sha256(context_bytes).hexdigest()
            status = 'completed_matches' if available else 'completed_no_match'
        except Exception as exc:
            # Exception messages may contain connection strings or service data.
            warning = type(exc).__name__
            status = 'unavailable'
            context = None
        missing = [value for value in required if value not in available]
        receipt = {'schema_version': 'AnalysisKnowledgeReceipt/v1', 'design_id': design_id, 'analysis_id': analysis_id, 'source_sha256': source_sha256, 'actor_id': manifest.actor_id, 'scope': scope, 'query': query.strip(), 'features_sha256': hashlib.sha256(feature_bytes).hexdigest(), 'context_sha256': context_sha, 'status': status, 'blocking': bool(missing), 'available_ids': available, 'required_ids': required, 'missing_required_ids': missing, 'warning_code': warning, 'retrieved_at': datetime.now(UTC).isoformat()}
        receipt = parse_record(canonical_record(receipt, schema_name='knowledge-receipt').decode('utf-8'), schema_name='knowledge-receipt')
        return {'receipt': receipt, 'context': context}
