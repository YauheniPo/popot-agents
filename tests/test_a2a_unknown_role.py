"""The A2A entry point only accepts paths for roles the worker was configured with."""

import asyncio
import unittest

from ax_local.api.a2a_sdk import DelegationBoundary


class FakeService:
    roles = {'backend_engineer'}


async def request(app, path):
    sent = []

    async def receive():
        return {'type': 'http.request', 'body': b''}

    async def send(message):
        sent.append(message)

    await app({'type': 'http', 'method': 'POST', 'path': path, 'headers': []}, receive, send)
    return sent


class UnknownRoleTests(unittest.TestCase):
    def test_unknown_role_path_is_not_found(self):
        sent = asyncio.run(request(DelegationBoundary(None, FakeService()), '/a2a/stranger'))
        start = [message for message in sent if message['type'] == 'http.response.start'][0]
        self.assertEqual(start['status'], 404)


if __name__ == '__main__':
    unittest.main()
