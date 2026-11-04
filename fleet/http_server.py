"""Bound active requests, not the number of registered browser users."""
from http.server import ThreadingHTTPServer
import threading


class BoundedServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(64)
        self.tls_context = None
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            request.settimeout(20)
            if self.tls_context:
                request = self.tls_context.wrap_socket(request, server_side=True)
            super().process_request_thread(request, client_address)
        except (OSError, ValueError):
            request.close()
        finally:
            self.slots.release()
