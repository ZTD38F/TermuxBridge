"""Isolated end-to-end rollback transaction against simulated localhost MCP services.

Runs on ephemeral ports: it never accesses the real phone's port 8765 or its
runtime directory, even when executed inside Termux.
"""
from __future__ import annotations
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.request
from unittest import mock

from runtime import rollback_guard as guard

GOOD="1"*40
BAD="2"*40
TOKEN="T"*40
CONTRACT=[{"name":"run_command","inputSchema":{"type":"object","properties":{},"required":[]}}]


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1",0))
        return s.getsockname()[1]


class RecoveryIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        for s in ("releases/"+GOOD,"releases/"+BAD,"state","secrets","logs","jobs"):
            (self.root/s).mkdir(parents=True)
        (self.root/"current").symlink_to(self.root/"releases"/BAD)
        (self.root/"previous").symlink_to(self.root/"releases"/GOOD)
        (self.root/"server.pid").write_text("999999\n")
        (self.root/"source_commit").write_text(BAD+"\n")
        for name in ("backend_token","router_token"):
            (self.root/"secrets"/name).write_text(TOKEN)
        self.broken_port,self.good_port,self.supervisor_port=port(),port(),port()
        while len({self.broken_port,self.good_port,self.supervisor_port})<3:
            self.broken_port,self.good_port,self.supervisor_port=port(),port(),port()
        guard.save_json(self.root/"state/route.json",{"generation":BAD,"port":self.broken_port})
        guard.save_json(self.root/"state/update.json",
                        {"phase":"COMMITTED","current_generation":BAD,"previous_generation":GOOD})
        guard.save_json(self.root/"state/maintenance.json",
                        {"consecutive_failures":3,"reason":"LOCAL_SERVICES_UNHEALTHY",
                         "local_processes":{"supervisor":True,"supervisor_http":True,"mcp_http":False}})
        script=textwrap.dedent('''
            from http.server import BaseHTTPRequestHandler, HTTPServer
            import json,os,sys
            class H(BaseHTTPRequestHandler):
                def log_message(self,*a): pass
                def _send(self,d):
                    raw=json.dumps(d).encode()
                    self.send_response(200)
                    self.send_header("Content-Length",str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                def do_GET(self):
                    self._send({"ok":True,"pid":os.getpid()})
                def do_POST(self):
                    self.rfile.read(int(self.headers.get("Content-Length","0")))
                    self._send({"jsonrpc":"2.0","id":101,"result":{
                        "tools":[{"name":"run_command","inputSchema":{
                            "type":"object","properties":{},"required":[]}}]}})
            HTTPServer(("127.0.0.1",int(sys.argv[2])),H).serve_forever()
        ''')
        (self.root/"releases"/GOOD/"bridge_server.py").write_text(script)
        (self.root/"releases"/BAD/"bridge_server.py").write_text("pass\n")
        parent=self

        class Supervisor(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,data):
                payload=json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Length",str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def do_GET(self):
                if self.headers.get("X-Bridge-Token")!=TOKEN:
                    self.send_error(403)
                    return
                state=guard.load(parent.root/"state/route.json")
                self.reply({"ok":True,"active_generation":state["generation"],
                            "active_port":state["port"],"inflight":{}})
            def do_POST(self):
                if self.headers.get("X-Bridge-Token")!=TOKEN:
                    self.send_error(403)
                    return
                length=int(self.headers["Content-Length"])
                payload=self.rfile.read(length)
                target=guard.load(parent.root/"state/route.json")["port"]
                request=urllib.request.Request(
                    f"http://127.0.0.1:{target}/mcp",data=payload,
                    headers={"X-Bridge-Backend-Token":TOKEN,"Content-Type":"application/json"})
                with urllib.request.urlopen(request,timeout=3) as rsp:
                    result=json.load(rsp)
                self.reply(result)

        self.srv=ThreadingHTTPServer(("127.0.0.1",self.supervisor_port),Supervisor)
        self.thread=threading.Thread(target=self.srv.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        try:
            pid=int((self.root/"server.pid").read_text())
            script=self.root/"releases"/GOOD/"bridge_server.py"
            if guard._pid_verified(pid,script,self.good_port):
                os.kill(pid,signal.SIGTERM)
                try: os.waitpid(pid,0)
                except ChildProcessError: pass
        except (OSError,ValueError):
            pass
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def test_three_failures_roll_back_and_quarantine_bad_release(self):
        with mock.patch.object(guard,"BACKEND_PORTS",(self.broken_port,self.good_port)), \
             mock.patch.object(guard,"SUPERVISOR_PORT",self.supervisor_port):
            result=guard.attempt(self.root)
        self.assertEqual(result["result"],"ROLLED_BACK",result)
        self.assertEqual(guard.load(self.root/"state/route.json")["generation"],GOOD)
        self.assertEqual(guard.load(self.root/"state/route.json")["port"],self.good_port)
        self.assertEqual((self.root/"current").resolve(),self.root/"releases"/GOOD)
        self.assertEqual((self.root/"previous").resolve(),self.root/"releases"/BAD)
        self.assertEqual((self.root/"source_commit").read_text().strip(),GOOD)
        self.assertEqual(guard.load(self.root/"state/update.json")["phase"],"COMMITTED")
        self.assertEqual(guard.load(self.root/"state/quarantine.json")["generation"],BAD)

    def test_two_failures_never_start_a_rollback_process(self):
        snapshot=guard.load(self.root/"state/maintenance.json")
        snapshot["consecutive_failures"]=2
        guard.save_json(self.root/"state/maintenance.json",snapshot)
        with mock.patch.object(guard,"BACKEND_PORTS",(self.broken_port,self.good_port)), \
             mock.patch.object(guard,"SUPERVISOR_PORT",self.supervisor_port):
            result=guard.attempt(self.root)
        self.assertEqual(result["result"],"NOT_ELIGIBLE")
        self.assertFalse((self.root/"state/quarantine.json").exists())


if __name__=="__main__":
    unittest.main()
