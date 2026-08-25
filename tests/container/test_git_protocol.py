import unittest

from agent_container.git_protocol import ZERO_OID_SHA1
from agent_container.git_protocol import decode_pkt_line_section
from agent_container.git_protocol import gate_receive_pack_commands
from agent_container.git_protocol import parse_receive_pack_advertisement
from agent_container.github_broker_policy import BrokerPolicy


def pkt(payload: bytes) -> bytes:
    return f"{len(payload) + 4:04x}".encode("ascii") + payload


FLUSH = b"0000"
OLD = "1" * 40
NEW = "2" * 40
OTHER = "3" * 40


def advertisement(
    *refs: tuple[str, str],
    capabilities: str = (
        "report-status report-status-v2 side-band-64k quiet atomic ofs-delta "
        "agent=github/1.0 object-format=sha1"
    ),
) -> bytes:
    selected = refs or ((OLD, "refs/heads/existing"),)
    lines = []
    for index, (oid, ref) in enumerate(selected):
        suffix = ("\0" + capabilities).encode("ascii") if index == 0 else b""
        lines.append(pkt(f"{oid} {ref}".encode("ascii") + suffix + b"\n"))
    return b"".join(lines) + FLUSH


def commands(
    *updates: tuple[str, str, str],
    capabilities: str = "report-status side-band-64k object-format=sha1",
    pack: bytes = b"PACKpayload",
) -> bytes:
    lines = []
    for index, (old, new, ref) in enumerate(updates):
        suffix = ("\0" + capabilities).encode("ascii") if index == 0 else b""
        lines.append(pkt(f"{old} {new} {ref}".encode("ascii") + suffix + b"\n"))
    return b"".join(lines) + FLUSH + pack


class PktLineTest(unittest.TestCase):
    def test_decodes_bounded_section_and_leaves_following_bytes(self) -> None:
        data = pkt(b"one\n") + pkt(b"two\n") + FLUSH + b"PACKrest"

        section, consumed = decode_pkt_line_section(data)

        self.assertEqual(section, (b"one\n", b"two\n"))
        self.assertEqual(data[consumed:], b"PACKrest")

    def test_rejects_invalid_or_incomplete_pkt_lines(self) -> None:
        cases = (
            b"",
            b"zzzz",
            b"0001",
            b"0002",
            b"0003",
            b"0008abc",
            pkt(b"one") + FLUSH,
        )
        for data in cases:
            with self.subTest(data=data[:12]):
                kwargs = {"maximum_packets": 0} if data.startswith(b"0007") else {}
                with self.assertRaises(ValueError):
                    decode_pkt_line_section(data, **kwargs)

    def test_rejects_packet_and_section_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "packet is too large"):
            decode_pkt_line_section(pkt(b"12345") + FLUSH, maximum_packet=8)
        with self.assertRaisesRegex(ValueError, "section is too large"):
            decode_pkt_line_section(
                pkt(b"1234") + pkt(b"5678") + FLUSH,
                maximum_section=10,
            )
        with self.assertRaisesRegex(ValueError, "too many packets"):
            decode_pkt_line_section(
                pkt(b"one") + pkt(b"two") + FLUSH,
                maximum_packets=1,
            )


class ReceivePackAdvertisementTest(unittest.TestCase):
    def test_parses_heads_and_negotiated_object_format(self) -> None:
        parsed = parse_receive_pack_advertisement(
            advertisement(
                (OLD, "HEAD"),
                (NEW, "refs/heads/main"),
                (OTHER, "refs/tags/v1"),
            )
        )

        self.assertEqual(parsed.object_format, "sha1")
        self.assertEqual(parsed.refs["refs/heads/main"], NEW)
        self.assertEqual(parsed.refs["HEAD"], OLD)
        self.assertEqual(parsed.consumed, len(advertisement(
            (OLD, "HEAD"),
            (NEW, "refs/heads/main"),
            (OTHER, "refs/tags/v1"),
        )))

    def test_supports_sha256_and_empty_repository_advertisement(self) -> None:
        sha256 = "a" * 64
        parsed = parse_receive_pack_advertisement(
            advertisement(
                ("0" * 64, "capabilities^{}"),
                capabilities="report-status object-format=sha256",
            )
        )
        self.assertEqual(parsed.object_format, "sha256")
        self.assertEqual(parsed.refs, {})
        self.assertNotIn("capabilities^{}", parsed.refs)

        populated = parse_receive_pack_advertisement(
            advertisement(
                (sha256, "refs/heads/main"),
                capabilities="report-status object-format=sha256",
            )
        )
        self.assertEqual(populated.refs["refs/heads/main"], sha256)

    def test_rejects_malformed_or_unexpected_advertisement(self) -> None:
        cases = (
            FLUSH,
            pkt(b"not-an-advertisement\n") + FLUSH,
            advertisement(capabilities="report-status object-format=md5"),
            advertisement(capabilities="report-status object-format=sha1 object-format=sha256"),
            advertisement((OLD, "refs/heads/bad ref")),
            pkt(f"{OLD} refs/heads/main\0report-status\n".encode())
            + pkt(f"{NEW} refs/heads/main\n".encode())
            + FLUSH,
        )
        for data in cases:
            with self.subTest(data=data[:60]):
                with self.assertRaises(ValueError):
                    parse_receive_pack_advertisement(data)


class ReceivePackGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = BrokerPolicy.create(
            project_id="agent-container",
            repository="jj1xgo/agent-container",
            default_branch="main",
            protected_branches=("main", "release/stable"),
        )
        self.advertisement = parse_receive_pack_advertisement(
            advertisement(
                (OLD, "refs/heads/existing"),
                (OTHER, "refs/heads/main"),
            )
        )

    def test_allows_existing_lease_and_new_work_branch(self) -> None:
        data = commands(
            (OLD, NEW, "refs/heads/existing"),
            (ZERO_OID_SHA1, OTHER, "refs/heads/feat/new"),
        )

        gated = gate_receive_pack_commands(data, self.advertisement, self.policy)

        self.assertEqual(len(gated.updates), 2)
        self.assertEqual(gated.updates[0].ref, "refs/heads/existing")
        self.assertEqual(gated.updates[1].old_oid, ZERO_OID_SHA1)
        self.assertEqual(data[gated.consumed:], b"PACKpayload")

    def test_rejects_delete_protected_non_head_duplicate_and_bad_lease(self) -> None:
        cases = (
            commands((OLD, ZERO_OID_SHA1, "refs/heads/existing")),
            commands((OTHER, NEW, "refs/heads/main")),
            commands((ZERO_OID_SHA1, NEW, "refs/tags/v2")),
            commands(
                (OLD, NEW, "refs/heads/existing"),
                (OLD, OTHER, "refs/heads/existing"),
            ),
            commands((OTHER, NEW, "refs/heads/existing")),
            commands((ZERO_OID_SHA1, NEW, "refs/heads/existing")),
            commands((OLD, NEW, "refs/heads/new")),
        )
        for data in cases:
            with self.subTest(data=data[:100]):
                with self.assertRaises(ValueError):
                    gate_receive_pack_commands(data, self.advertisement, self.policy)

    def test_rejects_unknown_or_risky_capabilities(self) -> None:
        for capability in (
            "unknown-feature",
            "push-options",
            "push-cert=nonce",
            "delete-refs",
            "agent=bad value",
        ):
            with self.subTest(capability=capability):
                data = commands(
                    (OLD, NEW, "refs/heads/existing"),
                    capabilities=f"report-status {capability}",
                )
                with self.assertRaisesRegex(ValueError, "capability"):
                    gate_receive_pack_commands(data, self.advertisement, self.policy)

    def test_rejects_capability_not_advertised_by_server(self) -> None:
        limited = parse_receive_pack_advertisement(
            advertisement(capabilities="report-status object-format=sha1")
        )
        data = commands(
            (OLD, NEW, "refs/heads/existing"),
            capabilities="report-status atomic object-format=sha1",
        )
        with self.assertRaisesRegex(ValueError, "capability"):
            gate_receive_pack_commands(data, limited, self.policy)

    def test_rejects_duplicate_capability_names(self) -> None:
        data = commands(
            (OLD, NEW, "refs/heads/existing"),
            capabilities="report-status agent=git/2.50 agent=git/2.51",
        )
        with self.assertRaisesRegex(ValueError, "capabilities"):
            gate_receive_pack_commands(data, self.advertisement, self.policy)

    def test_accepts_bounded_known_capabilities(self) -> None:
        data = commands(
            (OLD, NEW, "refs/heads/existing"),
            capabilities=(
                "report-status report-status-v2 side-band-64k quiet atomic "
                "ofs-delta agent=git/2.51.0 object-format=sha1"
            ),
        )
        gated = gate_receive_pack_commands(data, self.advertisement, self.policy)
        self.assertIn("report-status", gated.capabilities)
        self.assertIn("agent=git/2.51.0", gated.capabilities)

    def test_rejects_empty_commands_ref_limit_and_object_format_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            gate_receive_pack_commands(FLUSH + b"PACK", self.advertisement, self.policy)

        too_many = commands(
            *((ZERO_OID_SHA1, NEW, f"refs/heads/feat/{index}") for index in range(9))
        )
        with self.assertRaisesRegex(ValueError, "too many ref updates"):
            gate_receive_pack_commands(too_many, self.advertisement, self.policy)

        mismatch = commands(
            (OLD, NEW, "refs/heads/existing"),
            capabilities="report-status object-format=sha256",
        )
        with self.assertRaisesRegex(ValueError, "object format"):
            gate_receive_pack_commands(mismatch, self.advertisement, self.policy)

    def test_errors_do_not_echo_ref_or_capability_markers(self) -> None:
        marker = "secret-marker"
        data = commands(
            (ZERO_OID_SHA1, NEW, f"refs/heads/{marker}"),
            capabilities=f"report-status {marker}",
        )
        with self.assertRaises(ValueError) as raised:
            gate_receive_pack_commands(data, self.advertisement, self.policy)
        self.assertNotIn(marker, str(raised.exception))
