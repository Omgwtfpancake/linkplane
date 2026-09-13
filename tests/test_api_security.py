"""`linkplane.api.security`: bind, Host/Origin, bearer, status mapping, error bodies, redaction."""

import logging
import unittest
from io import StringIO

from linkplane.api import security
from linkplane.core import errors
from linkplane.operations import OperationCancelled
from linkplane.transports import BridgeError


class BindTests(unittest.TestCase):
    def test_only_loopback_binds_are_accepted(self):
        for ok in ("127.0.0.1", "127.0.0.2", "localhost", "::1", "[::1]", " 127.0.0.1 "):
            self.assertTrue(security.is_loopback(ok), ok)
            security.check_bind_address(ok)
        for bad in ("0.0.0.0", "::", "198.51.100.5", "10.0.0.1", "example.com", "", "phone.local"):
            self.assertFalse(security.is_loopback(bad), bad)
            with self.assertRaises(errors.LinkplaneError) as raised:
                security.check_bind_address(bad)
            self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)
            self.assertIn("remote binding is not supported", str(raised.exception))


class HeaderTests(unittest.TestCase):
    def test_host_must_be_loopback_and_the_bound_port(self):
        for ok in ("127.0.0.1:8741", "localhost:8741", "[::1]:8741", "LOCALHOST:8741"):
            self.assertTrue(security.valid_host_header(ok, 8741), ok)
        for bad in (None, "", "127.0.0.1", "127.0.0.1:8742", "evil.example:8741", "198.51.100.2:8741", "[::1]"):
            self.assertFalse(security.valid_host_header(bad, 8741), bad)
        self.assertTrue(security.valid_host_header("localhost", 80))

    def test_origin_is_refused_unless_allow_listed(self):
        self.assertTrue(security.origin_allowed(None))
        self.assertFalse(security.origin_allowed("http://localhost:3000"))
        self.assertFalse(security.origin_allowed("null"))
        self.assertTrue(security.origin_allowed("http://localhost:3000/", ("http://localhost:3000",)))

    def test_check_request_headers_is_403_client_before_authentication(self):
        security.check_request_headers({"Host": "127.0.0.1:8741"}, port=8741)
        for headers in ({"Host": "evil:8741"}, {"Host": "127.0.0.1:8741", "Origin": "http://evil"}):
            with self.assertRaises(errors.LinkplaneError) as raised:
                security.check_request_headers(headers, port=8741)
            self.assertEqual(raised.exception.code, errors.CLIENT_UNAUTHENTICATED)


class BearerTests(unittest.TestCase):
    def test_header_is_the_only_place(self):
        self.assertEqual(security.bearer_token({"Authorization": "Bearer abc.def"}), "abc.def")
        self.assertEqual(security.bearer_token({"Authorization": "bearer  xyz "}), "xyz")
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer"}, {"Authorization": "Bearer a b"}):
            with self.assertRaises(errors.LinkplaneError) as raised:
                security.bearer_token(headers)
            self.assertEqual(raised.exception.code, errors.CLIENT_UNAUTHENTICATED)

    def test_token_in_url_is_refused_even_when_the_header_is_valid(self):
        for name in ("token", "access_token", "bearer"):
            with self.assertRaises(errors.LinkplaneError) as raised:
                security.bearer_token({"Authorization": "Bearer good"}, {name: ["good"]})
            self.assertEqual(raised.exception.code, errors.REQUEST_INVALID)

    def test_correlation_id_is_opaque_bounded_or_generated(self):
        self.assertEqual(security.correlation_id_from("ci-291"), "ci-291")
        self.assertEqual(len(security.correlation_id_from(None)), 32)
        for bad in ("", "x" * 129, 42, "line\nbreak"):
            with self.assertRaises(errors.LinkplaneError):
                security.correlation_id_from(bad)


class MappingTests(unittest.TestCase):
    def test_every_lp_code_has_a_deliberate_status(self):
        codes = {v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str) and v.startswith("LP-")}
        unmapped = codes - set(security.STATUS_FOR_CODE)
        self.assertEqual(unmapped, set(), "add each new code to STATUS_FOR_CODE deliberately")

    def test_design_table_rows(self):
        table = {
            errors.REQUEST_INVALID: 400, errors.CLIENT_UNAUTHENTICATED: 401, errors.CLIENT_FORBIDDEN: 403,
            errors.DEVICE_NOT_FOUND: 404, errors.RESOURCE_NOT_FOUND: 404,
            errors.STATE_CONFLICT: 409, errors.CAPABILITY_UNAVAILABLE: 409, errors.CAPABILITY_UNSUPPORTED: 409,
            errors.CONNECT_AMBIGUOUS: 409, errors.CONFIG_MISSING: 500, errors.CONFIG_INVALID: 500,
            errors.INTERNAL: 500, errors.AUTH_FAILED: 502, errors.PROVIDER_FAILED: 502, errors.PROVIDER_BAD_OUTPUT: 502,
            errors.PROVIDER_PARTIAL: 502, errors.CONNECT_UNREACHABLE: 503, errors.CONNECT_NO_DEVICE: 503,
            errors.AUTH_UNAUTHORIZED_DEVICE: 503, errors.DEPENDENCY_MISSING: 503, errors.CANCELLED: 503,
            errors.TIMEOUT: 504,
        }
        for code, status in table.items():
            self.assertEqual(security.status_for(code), status, code)
        self.assertEqual(security.status_for(errors.CAPABILITY_UNSUPPORTED, not_implemented=True), 501)
        self.assertEqual(security.status_for("LP-FUTURE-999"), 500)

    def test_response_headers(self):
        self.assertIn("WWW-Authenticate", security.response_headers(401))
        self.assertEqual(security.response_headers(503, errors.CONNECT_NO_DEVICE)["Retry-After"], "5")
        self.assertEqual(security.response_headers(200)["Linkplane-Api"], "1")


class ErrorBodyTests(unittest.TestCase):
    def test_linkplane_error_body_keeps_the_existing_object(self):
        status, body = security.error_body(errors.LinkplaneError(errors.CONNECT_NO_DEVICE, "no ADB device", ("plug it in",)),
                                           correlation_id="c1", details={"device": "phone"})
        self.assertEqual(status, 503)
        self.assertEqual(body, {"error": {"type": "LinkplaneError", "message": "no ADB device", "code": "LP-CONNECT-002",
                                          "hints": ["plug it in"], "correlation_id": "c1", "details": {"device": "phone"}}})

    def test_plain_bridge_errors_are_classified_and_cancellation_is_coded(self):
        status, body = security.error_body(BridgeError("ssh timed out after 8 seconds"), correlation_id="c")
        self.assertEqual((status, body["error"]["code"]), (504, errors.TIMEOUT))
        status, body = security.error_body(OperationCancelled("stopping"), correlation_id="c")
        self.assertEqual((status, body["error"]["code"]), (503, errors.CANCELLED))

    def test_unexpected_exceptions_never_leak_their_text(self):
        status, body = security.error_body(RuntimeError("/home/user/.ssh/id_ed25519: Permission denied"), correlation_id="c")
        self.assertEqual((status, body["error"]["code"]), (500, errors.INTERNAL))
        self.assertEqual(body["error"]["message"], security.INTERNAL_MESSAGE)
        self.assertNotIn("id_ed25519", str(body))

    def test_not_implemented_is_501(self):
        status, _body = security.error_body(errors.LinkplaneError(errors.CAPABILITY_UNSUPPORTED, "x"), correlation_id="c", not_implemented=True)
        self.assertEqual(status, 501)


class RedactionTests(unittest.TestCase):
    def test_tokens_never_reach_a_log_line(self):
        logger = logging.getLogger("linkplane.api.test")
        logger.setLevel(logging.DEBUG)
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger.addHandler(handler)
        logger.addFilter(security.RedactingFilter())
        try:
            logger.debug("headers: Authorization: Bearer SECRET-TOKEN-123 host=%s", "127.0.0.1")
            logger.warning("failed with header %s", "authorization=SECRET2")
            logger.info("Bearer SECRET3 elsewhere")
        finally:
            logger.removeHandler(handler)
        text = stream.getvalue()
        self.assertNotIn("SECRET", text)
        self.assertIn("[redacted]", text)
        self.assertEqual(security.redact("x Bearer abc y"), "x Bearer [redacted] y")
