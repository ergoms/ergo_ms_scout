"""Локальный HTML-стенд для живого прогона SCOUT. Django и inline-исполнитель не используются."""
import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

hits = Counter()
gate = threading.Event()
lock = threading.Lock()


class Site(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/release':
            gate.set()
        elif self.path == '/reset':
            with lock:
                hits.clear()
            gate.clear()
        self.reply(200, b'{}', 'application/json')

    def do_GET(self):
        path = self.path
        if path == '/stats':
            with lock:
                data = dict(hits)
            self.reply(200, json.dumps(data).encode(), 'application/json')
            return
        with lock:
            hits[path] += 1
            count = hits[path]
        host = self.headers.get('Host') or '127.0.0.1:8767'
        if path in ('/redir', '/chain', '/chain-mid'):
            target = {'/redir': '/land', '/chain': '/chain-mid', '/chain-mid': '/chain-land'}[path]
            self.send_response(302)
            self.send_header('Location', f'http://{host}{target}')
            self.end_headers()
            return
        if path == '/redirect/out':
            self.send_response(302)
            self.send_header('Location', 'http://localhost:8767/outside')
            self.end_headers()
            return
        if path.startswith('/retry/') and count == 1:
            if path.endswith('timeout'):
                time.sleep(21)
            else:
                self.reply(int(path.rsplit('/', 1)[1]), b'busy', 'text/html')
                return
        if path == '/slow/child':
            gate.wait(18)
        links = ''
        if path == '/cycle/a':
            links = '<a href="b">B</a><a href="/cycle/a#x">A</a><a href="/cycle/c">C</a>'
        elif path == '/cycle/b':
            links = '<a href="/cycle/a">A</a><a href="/cycle/c#z">C</a>'
        elif path == '/cycle/c':
            links = '<a href="/cycle/a">A</a>'
        elif path == '/storm':
            links = ''.join(f'<a href="/skip/{i}.pdf">pdf</a>' for i in range(60))
        elif path == '/slow/start':
            links = '<a href="/slow/child">child</a>'
        elif path == '/slow/child':
            links = '<a href="/slow/grandchild">grandchild</a>'
        elif path.startswith('/depth/'):
            number = int(path.rsplit('/', 1)[1])
            links = f'<a href="/depth/{number + 1}">next</a>' if number < 6 else ''
        elif path == '/wide':
            links = ''.join(f'<a href="/wide/{i}">next</a>' for i in range(10))
        elif path == '/base':
            links = '<base href="/items/"><a href="one?id=9&page=2&id=10">item</a><a href="one?id=9&page=2&id=10#x">duplicate</a>'
        elif path == '/catalog':
            links = '<a href="/details/1">one</a><a href="/details/2">two</a>'
        elif path == '/hop':
            links = '<a href="/redir">r</a>'
        elif path == '/land':
            links = '<a href="/next">n</a>'
        elif path == '/chain-start':
            links = '<a href="/chain">c</a>'
        elif path == '/chain-land':
            links = '<a href="/chain-next">n</a>'
        elif path == '/diamond':
            links = '<a href="/long">l</a><a href="/short">s</a>'
        elif path == '/long':
            links = '<a href="/dmid">m</a>'
        elif path == '/dmid':
            links = '<a href="/target">t</a>'
        elif path == '/short':
            links = '<a href="/target">t</a>'
        elif path == '/target':
            links = '<a href="/leaf">f</a>'
        body = f'<!doctype html><meta charset="utf-8"><h1>{path}</h1>{links}'.encode()
        self.reply(200, body, 'text/html; charset=utf-8')

    def reply(self, code, body, media):
        self.send_response(code)
        self.send_header('Content-Type', media)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        return


print('Fixture 127.0.0.1:8767', flush=True)
ThreadingHTTPServer(('127.0.0.1', 8767), Site).serve_forever()
