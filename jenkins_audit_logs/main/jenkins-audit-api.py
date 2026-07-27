#!/usr/bin/env python3
import http.server
import socketserver
import json
import subprocess
import os
from urllib.parse import urlparse, parse_qs

PORT = 9090
LOG_FILE = "/var/lib/jenkins/jenkins-1year-archive/audit-logs/audit-combined-all.log"
DASHBOARD_FILE = "/var/lib/jenkins/jenkins-1year-archive/jenkins-audit-dashboard.html"

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        
        try:
            if path == '/api/stats':
                self.send_stats()
            elif path == '/api/logs':
                self.send_logs()
            elif path == '/api/search':
                query = parse_qs(urlparse(self.path).query)
                self.send_search(query)
            elif path == '/api/role-diff':
                self.send_role_diff()
            else:
                self.send_dashboard()
        except Exception as e:
            self.send_error(500, str(e))
    
    def send_stats(self):
        """Get LIVE stats from audit log"""
        try:
            result = subprocess.run(f"wc -l {LOG_FILE} | awk '{{print $1}}'", 
                                  shell=True, capture_output=True, text=True, timeout=5)
            total = result.stdout.strip() or "0"
            
            result = subprocess.run(f"grep -c 'configSubmit' {LOG_FILE}", 
                                  shell=True, capture_output=True, text=True, timeout=5)
            config = int(result.stdout.strip() or "0")
            
            result = subprocess.run(f"grep -c 'rebuild' {LOG_FILE}", 
                                  shell=True, capture_output=True, text=True, timeout=5)
            rebuild = int(result.stdout.strip() or "0")
            
            result = subprocess.run(f"grep -c 'createItem' {LOG_FILE}", 
                                  shell=True, capture_output=True, text=True, timeout=5)
            creates = int(result.stdout.strip() or "0")
            
            result = subprocess.run(f"grep -c 'used credentials' {LOG_FILE}", 
                                  shell=True, capture_output=True, text=True, timeout=5)
            creds = int(result.stdout.strip() or "0")
            
            result = subprocess.run(f"grep -iE 'manage/role|rolesSubmit|assign-roles' {LOG_FILE} | wc -l", 
                                  shell=True, capture_output=True, text=True, timeout=5)
            roles = int(result.stdout.strip() or "0")
            
            result = subprocess.run(f"du -sh /var/lib/jenkins/jenkins-1year-archive | awk '{{print $1}}'", 
                                  shell=True, capture_output=True, text=True, timeout=5)
            archive = result.stdout.strip() or "0M"
            
            data = {
                "status": "success",
                "total_logs": total,
                "total_users": "21",
                "archive_size": archive,
                "actions": {
                    "configSubmit": config,
                    "rebuild": rebuild,
                    "createItem": creates,
                    "credentials": creds,
                    "role": roles
                }
            }
            self.send_json(data)
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})
    
    def send_logs(self):
        """Get ALL log entries from the file"""
        try:
            result = subprocess.run(f"cat {LOG_FILE}", 
                                  shell=True, capture_output=True, text=True, timeout=10)
            logs = [l for l in result.stdout.split('\n') if l.strip()]
            self.send_json({"status": "success", "total": len(logs), "logs": logs})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})
    

    def send_role_diff(self):
        """Get role change diffs from config-history"""
        import glob
        try:
            config_dir = "/var/lib/jenkins/config-history/config"
            dirs = sorted([d for d in os.listdir(config_dir) if os.path.isdir(os.path.join(config_dir, d))])
            results = []
            for i in range(len(dirs)-1, 0, -1):
                newer = os.path.join(config_dir, dirs[i], "config.xml")
                older = os.path.join(config_dir, dirs[i-1], "config.xml")
                history = os.path.join(config_dir, dirs[i], "history.xml")
                user = "unknown"
                if os.path.exists(history):
                    with open(history) as hf:
                        hcontent = hf.read()
                        import re
                        um = re.search(r'<user>(.*?)</user>', hcontent)
                        if um: user = um.group(1)
                if not os.path.exists(newer) or not os.path.exists(older):
                    continue
                result = subprocess.run(["diff", older, newer], capture_output=True, text=True)
                added = []
                removed = []
                for line in result.stdout.split("\n"):
                    if line.startswith(">"):
                        val = line[1:].strip()
                        if any(k in val for k in ["sid", "role name", "permission"]):
                            added.append(val)
                    elif line.startswith("<"):
                        val = line[1:].strip()
                        if any(k in val for k in ["sid", "role name", "permission"]):
                            removed.append(val)
                if added or removed:
                    results.append({
                        "timestamp": dirs[i].replace("_", " ").replace("-", "/", 2).replace("-", ":"),
                        "user": user,
                        "added": added,
                        "removed": removed
                    })
            self.send_json({"status": "success", "changes": results})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})

    def send_search(self, query):
        """Search logs by username or jobname"""
        try:
            username = query.get('username', [''])[0]
            jobname = query.get('jobname', [''])[0]
            
            if username:
                cmd = f"grep -i '{username}' {LOG_FILE} | tail -100"
            elif jobname:
                cmd = f"grep -i '{jobname}' {LOG_FILE} | tail -100"
            else:
                self.send_json({"status": "error", "message": "No search criteria"})
                return
            
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            results = [l for l in result.stdout.split('\n') if l.strip()]
            self.send_json({"status": "success", "results": results, "count": len(results)})
        except Exception as e:
            self.send_json({"status": "error", "message": str(e)})
    
    def send_dashboard(self):
        """Serve the HTML dashboard"""
        try:
            if os.path.exists(DASHBOARD_FILE):
                with open(DASHBOARD_FILE, 'rb') as f:
                    content = f.read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
            else:
                self.send_response(404)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b"Dashboard file not found")
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(str(e).encode())
    
    def send_json(self, data):
        """Send JSON response"""
        response = json.dumps(data).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(response)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(response)
    
    def log_message(self, format, *args):
        """Suppress logging"""
        pass

if __name__ == '__main__':
    print(f"✓ API running on port {PORT}")
    print(f"✓ Dashboard: http://localhost:{PORT}/jenkins-audit-dashboard.html")
    print(f"✓ Reading logs from: {LOG_FILE}")
    
    with socketserver.TCPServer(("0.0.0.0", PORT), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n✓ Server stopped")
