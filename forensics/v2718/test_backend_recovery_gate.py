"""V2.7.18 backend-gate counterexample; real WSGI/store, no network.

This models an acknowledged workflow that terminates without canonical data.
It is not an assertion about Black Mirror or Virgin River's observed cause.
"""
from pathlib import Path
import io
import json
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'backend'))
from service import App, RequestStore


class WorkflowModel:
    def __init__(self):
        self.dispatches=[]
        self.active=False
        self.canonical_fact=False
    def dispatch(self,tmdb_id):
        self.dispatches.append(tmdb_id)
        self.active=True
    def fail_without_publication(self):
        self.active=False
        self.canonical_fact=False


class BackendGateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=1_000_000.0
        self.path=str(Path(self.temp.name)/'requests.sqlite')
        self.workflow=WorkflowModel()
        self.store=self.new_store()
        self.timeline=[]
    def new_store(self):
        return RequestStore(self.path,b'synthetic-gate-key',clock=lambda:self.now)
    def post(self,body=None):
        raw=json.dumps(body or {'tmdbId':88324}).encode()
        statuses=[]
        result=b''.join(App(self.store,self.workflow)({'PATH_INFO':'/v1/series-requests',
            'REQUEST_METHOD':'POST','CONTENT_TYPE':'application/json','CONTENT_LENGTH':str(len(raw)),
            'wsgi.input':io.BytesIO(raw),'REMOTE_ADDR':'127.0.0.1'},lambda status,headers:statuses.append(status)))
        response=json.loads(result)
        self.timeline.append({'elapsedSeconds':self.now-1_000_000,'http':statuses[0],
            'response':response,'dispatchCount':len(self.workflow.dispatches),
            'usefulWorkflowActive':self.workflow.active,'canonicalFactExists':self.workflow.canonical_fact})
        return statuses[0],response
    def failed_acknowledged_request(self):
        self.assertEqual(self.post()[1]['state'],'queued')
        self.workflow.fail_without_publication()
    def test_restarts_and_retries_cannot_recover_failed_work_in_minutes(self):
        self.failed_acknowledged_request()
        for elapsed in (30,90,210,240):
            self.now=1_000_000+elapsed
            self.store=self.new_store()  # durable backend record survives recreation
            self.assertEqual(self.post()[1]['state'],'already_queued')
            self.assertEqual(len(self.workflow.dispatches),1)
            self.assertFalse(self.workflow.active)
            self.assertFalse(self.workflow.canonical_fact)
    def test_waiting_past_24h_can_redispatch_but_is_not_minutes_recovery(self):
        self.failed_acknowledged_request()
        self.now+=86400
        self.assertEqual(self.post()[1]['state'],'already_queued')
        self.now+=1
        self.assertEqual(self.post()[1]['state'],'queued')
        self.assertEqual(len(self.workflow.dispatches),2)
    def test_android_cannot_force_retry_using_an_extra_body_field(self):
        self.failed_acknowledged_request()
        self.now+=240
        self.assertEqual(self.post({'tmdbId':88324,'forceRetry':True})[0],'400 Bad Request')
        self.assertEqual(len(self.workflow.dispatches),1)


if __name__=='__main__':
    unittest.main()
