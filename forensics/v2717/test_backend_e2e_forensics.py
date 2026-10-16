"""CURRENT_BEHAVIOR_TEST: real backend store/dispatcher, local SQLite and fake APIs.

No provider or production backend request is performed. No desired future
behavior is asserted. Run: python -m unittest discover -s forensics/v2717 -p 'test_*.py'.
"""
from pathlib import Path
import io
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from service import App, Config, DispatchFailure, GitHubDispatcher, RequestStore


class FakeDispatcher:
    def __init__(self, status=None): self.calls=[];self.status=status
    def dispatch(self,tmdb_id):
        self.calls.append(tmdb_id)
        if self.status is not None: raise DispatchFailure(self.status)


class BackendE2ECurrentBehavior(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.time=1_000_000
        self.store=RequestStore(str(Path(self.temp.name)/'requests.sqlite'),b'synthetic-forensic-key',clock=lambda:self.time)
    def send(self,dispatcher):
        response=[]
        body=b'{"tmdbId":88324}'
        payload=b''.join(App(self.store,dispatcher)({'PATH_INFO':'/v1/series-requests','REQUEST_METHOD':'POST',
            'CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(body)),'wsgi.input':io.BytesIO(body),'REMOTE_ADDR':'127.0.0.1'},lambda status,headers:response.append(status)))
        return response[0],json.loads(payload)
    def test_scenario12_dispatch_failure_is_not_accepted_and_can_be_retried(self):
        failed=FakeDispatcher(503)
        self.assertEqual(self.send(failed),('503 Service Unavailable',{'error':'dispatch_unavailable'}))
        good=FakeDispatcher()
        self.assertEqual(self.send(good)[1]['state'],'queued')
        self.assertEqual(good.calls,[88324])
    def test_nonretryable_github_failure_returns_424_without_dedup_entry(self):
        self.assertEqual(self.send(FakeDispatcher(403))[0],'424 Failed Dependency')
        self.assertEqual(self.send(FakeDispatcher())[1]['state'],'queued')
    def test_scenario13_dedup_ack_does_not_dispatch_or_prove_running_workflow(self):
        api=FakeDispatcher();self.assertEqual(self.send(api)[1]['state'],'queued')
        # A completed/failed/lost workflow is unobservable to RequestStore.
        self.assertEqual(self.send(api)[1],{'accepted':True,'tmdbId':88324,'state':'already_queued'})
        self.assertEqual(api.calls,[88324])
    def test_request_dead_state_possible_until_cooldown_expires(self):
        api=FakeDispatcher();self.send(api)
        self.time+=86400
        self.assertEqual(self.send(api)[1]['state'],'already_queued')
        self.time+=1
        self.assertEqual(self.send(api)[1]['state'],'queued')
        self.assertEqual(api.calls,[88324,88324])
    def test_github_dispatch_204_is_only_ack_not_workflow_completion(self):
        calls=[]
        class Response:
            status=204
            def __enter__(self):return self
            def __exit__(self,*args):return False
        def opener(req,timeout):
            calls.append((req.get_method(),req.full_url,json.loads(req.data),timeout));return Response()
        GitHubDispatcher(Config('synthetic/project','fixture-only'),opener=opener).dispatch(88324)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],'POST')
        self.assertEqual(calls[0][2],{'ref':'main','inputs':{'tmdb_id':'88324'}})
        self.assertEqual(calls[0][3],8)


if __name__=='__main__':unittest.main()
