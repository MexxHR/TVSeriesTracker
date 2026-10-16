"""Verify runner authentication using an invalid completion request, never a snapshot/ACK.

The deployed backend validates authentication before rejecting {} with
400 invalid_completion_request, before calling RequestStore.complete().
No secret, receipt, proof, response body or exception text is logged.
"""
import json
import os
from urllib import error, request

from controlled_batch import BatchClient, BatchFailure, strict_object


class AuthProbeFailure(Exception):
    """Only fixed, non-sensitive diagnostic codes are exposed."""


def _verify(base_url, expected_host, secret, opener=None):
    try:
        client = BatchClient(base_url, expected_host, secret, opener=opener)
    except Exception:
        raise AuthProbeFailure('CONFIG_REJECTED') from None
    url = client.base + '/internal/series-batch/complete'
    req = request.Request(url, data=b'{}', method='POST', headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + client.secret})
    try:
        with client.opener.open(req, timeout=8):
            # Success is intentionally NOT a 2xx response: {} must never ACK.
            raise AuthProbeFailure('UNEXPECTED_ACCEPTANCE')
    except error.HTTPError as response:
        with response:
            if response.geturl() != url:
                raise AuthProbeFailure('DESTINATION_REJECTED') from None
            if response.code == 401:
                raise AuthProbeFailure('AUTH_REJECTED') from None
            if response.code == 404:
                raise AuthProbeFailure('MODE_OR_ROUTE_REJECTED') from None
            if response.code != 400 or response.headers.get_content_type() != 'application/json':
                raise AuthProbeFailure('UNEXPECTED_RESPONSE') from None
            raw = response.read(2049)
            if len(raw) > 2048:
                raise AuthProbeFailure('UNEXPECTED_RESPONSE') from None
            try:
                data = json.loads(raw, object_pairs_hook=strict_object)
            except Exception:
                raise AuthProbeFailure('UNEXPECTED_RESPONSE') from None
            if data == {'error': 'https_required'}:
                raise AuthProbeFailure('HTTPS_REJECTED') from None
            if data != {'error': 'invalid_completion_request'}:
                raise AuthProbeFailure('UNEXPECTED_RESPONSE') from None
            return True
    except AuthProbeFailure:
        raise
    except BatchFailure:
        raise AuthProbeFailure('REDIRECT_OR_BOUNDARY_REJECTED') from None
    except Exception:
        raise AuthProbeFailure('TRANSPORT_FAILURE') from None


def verify(base_url, expected_host, secret, opener=None):
    try:
        return _verify(base_url, expected_host, secret, opener)
    except AuthProbeFailure:
        raise
    except Exception:
        # Includes failures while reading/closing a rejected HTTP response.
        raise AuthProbeFailure('TRANSPORT_FAILURE') from None


def main():
    try:
        verify(os.getenv('SERIES_BATCH_API_URL', ''), os.getenv('SERIES_BATCH_API_HOST', ''),
               os.getenv('SERIES_BATCH_API_SECRET', ''))
    except AuthProbeFailure as failure:
        print(json.dumps({'authenticated': False, 'code': str(failure)}))
        return 1
    print(json.dumps({'authenticated': True, 'code': 'AUTHENTICATED_INVALID_PAYLOAD_REJECTED',
                      'snapshotCalled': False, 'validAckSent': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
