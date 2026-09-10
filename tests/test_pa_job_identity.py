"""Runtime and deployed TGG case tools accept the same job-number width."""
import pytest

from tools.pa_business_tools import PABusinessOperation, _validate_operation_payload, _JOB_NO_RE


def test_supported_case_identity_survives_runtime_validation_unchanged():
    operation = PABusinessOperation(name='tgg_whatsapp_case_context', kind='http', path_params=('jobNo',))
    for job in ('SK/JOB/2604/2376', 'SK/JOB/2609/00156', 'SK/JOB/2609/156'):
        payload = {'jobNo': job}
        _validate_operation_payload(operation, payload)
        assert payload == {'jobNo': job}
        assert _JOB_NO_RE.findall(f'Please inspect {job}.') == [job]
    with pytest.raises(ValueError, match='INVALID_JOB_NO'):
        _validate_operation_payload(operation, {'jobNo': 'SK/JOB/2609/123456'})
