import Foundation

/// Reads the exact bytes that are about to be signed, before any key is touched.
///
/// The backend can be wrong or compromised, so nothing the operator sees and
/// nothing the signer checks comes from a backend summary: it all comes from
/// parsing these bytes (D-10, P-19). The parser is deliberately stricter than
/// JSONDecoder: duplicate keys, floats, trailing bytes, whitespace and non-ASCII
/// bytes are all refused, because the VM and the backend only ever emit
/// canonical ASCII JSON (sorted keys, no whitespace).

nonisolated enum SignedPurpose: String, Equatable, Sendable {
    case relayOrder = "growin.relay.order"
    case paperDispatch = "growin.execution.dispatch"
    case controlClear = "growin.execution.control.clear"
}

nonisolated enum SignedPayloadInspectionError: LocalizedError, Equatable {
    case malformed
    case duplicateKey
    case floatNotAllowed
    case trailingBytes
    case unknownPurpose
    case unsupportedVersion
    case missingField(String)
    case invalidField(String)
    case liveModeRefused

    var errorDescription: String? {
        switch self {
        case .malformed:
            return "The bytes to sign are not valid JSON, so nothing was signed."
        case .duplicateKey:
            return "The bytes to sign repeat a field name, so nothing was signed."
        case .floatNotAllowed:
            return "The bytes to sign contain a decimal number where a whole number or text belongs, so nothing was signed."
        case .trailingBytes:
            return "The bytes to sign carry extra data after the document, so nothing was signed."
        case .unknownPurpose:
            return "The bytes to sign have a purpose this app does not recognise, so nothing was signed."
        case .unsupportedVersion:
            return "The bytes to sign use a version this app does not support, so nothing was signed."
        case .missingField(let name):
            return "The bytes to sign are missing the field \(name), so nothing was signed."
        case .invalidField(let name):
            return "The bytes to sign have an invalid value for \(name), so nothing was signed."
        case .liveModeRefused:
            return "This order asks for live mode. Live orders are disabled, so nothing was signed."
        }
    }
}

// MARK: - Parsed shapes

/// The O3 intent as it sits inside the signed bytes (growin-orders/1).
nonisolated struct RelayOrderIntentFields: Equatable, Sendable {
    let intentId: String
    let proposalId: String
    let workspace: String
    let broker: String
    let mode: String
    let exchange: String
    let product: String
    let orderType: String
    let validity: String
    let side: String
    let stockCode: String
    let isin: String
    let quantity: Int
    let limitPrice: String
    let reason: String
    let batchId: String?
    let limitsSha256: String
    let paramsSha256: String
    let keyId: String
}

/// The O4 envelope: VM-minted bytes for one relay order.
nonisolated struct RelayOrderPayload: Equatable, Sendable {
    let challengeId: String
    let nonce: String
    let issuedAt: Int
    let expiresAt: Int
    let keyId: String
    let limitsSha256: String
    let intent: RelayOrderIntentFields
}

/// Only the fields the review and the Touch ID prompt need. The backend payload
/// carries more; extra keys are allowed here because the paper contract grows.
nonisolated struct PaperDispatchPayload: Equatable, Sendable {
    let challengeId: String
    let proposalId: String
    let workspace: String
    let mode: String
    let ticker: String
    let side: String
    let quantity: String
    let orderType: String?
    let limitPrice: String?
    let keyId: String
    let expiresAt: Int
}

nonisolated struct ControlClearPayload: Equatable, Sendable {
    let challengeId: String
    let workspace: String
    let controlVersion: Int
    let keyId: String
    let expiresAt: Int
}

nonisolated enum InspectedPayload: Equatable, Sendable {
    case relayOrder(RelayOrderPayload)
    case paperDispatch(PaperDispatchPayload)
    case controlClear(ControlClearPayload)

    var purpose: SignedPurpose {
        switch self {
        case .relayOrder: return .relayOrder
        case .paperDispatch: return .paperDispatch
        case .controlClear: return .controlClear
        }
    }

    var keyID: String {
        switch self {
        case .relayOrder(let payload): return payload.keyId
        case .paperDispatch(let payload): return payload.keyId
        case .controlClear(let payload): return payload.keyId
        }
    }

    var workspace: String {
        switch self {
        case .relayOrder(let payload): return payload.intent.workspace
        case .paperDispatch(let payload): return payload.workspace
        case .controlClear(let payload): return payload.workspace
        }
    }

    /// The text Touch ID shows. Built from the parsed bytes only.
    var touchPrompt: String {
        switch self {
        case .relayOrder(let payload):
            let intent = payload.intent
            return "approve India relay order: \(intent.side.uppercased()) \(intent.quantity) \(intent.stockCode) at limit \(intent.limitPrice)"
        case .paperDispatch(let payload):
            var text = "approve India paper order: \(payload.side.uppercased()) \(payload.quantity) \(payload.ticker)"
            if let limit = payload.limitPrice, !limit.isEmpty {
                text += " at limit \(limit)"
            }
            return text
        case .controlClear(let payload):
            return "clear the \(payload.workspace) trading control (version \(payload.controlVersion))"
        }
    }
}

// MARK: - Inspector

nonisolated enum SignedPayloadInspector {
    static let signedVersion = 1

    private static let relayTopKeys: Set<String> = [
        "version", "purpose", "challenge_id", "nonce", "issued_at", "expires_at",
        "key_id", "limits_sha256", "intent",
    ]
    private static let relayIntentKeys: Set<String> = [
        "intent_id", "proposal_id", "workspace", "broker", "mode", "exchange", "product",
        "order_type", "validity", "side", "stock_code", "isin", "quantity", "limit_price",
        "reason", "batch_id", "limits_sha256", "params_sha256", "key_id",
    ]
    private static let relayReasons: Set<String> = ["entry", "exit", "halve", "flatten", "stop"]
    private static let relayConstants: [(String, String)] = [
        ("workspace", "india"), ("broker", "icici-breeze"), ("exchange", "NSE"),
        ("product", "cash"), ("order_type", "limit"), ("validity", "day"),
    ]

    /// Parses the bytes and returns the typed view. Throws before any key access.
    static func inspect(_ bytes: Data) throws -> InspectedPayload {
        let root = try StrictJSONParser.parse(bytes)
        guard case .object(let object) = root else {
            throw SignedPayloadInspectionError.malformed
        }
        // Purpose first: an unknown purpose is refused no matter what else is wrong.
        guard case .string(let purposeText)? = object["purpose"] else {
            throw SignedPayloadInspectionError.missingField("purpose")
        }
        guard let purpose = SignedPurpose(rawValue: purposeText) else {
            throw SignedPayloadInspectionError.unknownPurpose
        }
        guard case .integer(let version)? = object["version"] else {
            throw SignedPayloadInspectionError.missingField("version")
        }
        guard version == signedVersion else {
            throw SignedPayloadInspectionError.unsupportedVersion
        }
        switch purpose {
        case .relayOrder:
            return .relayOrder(try relayPayload(object))
        case .paperDispatch:
            return .paperDispatch(try paperPayload(object))
        case .controlClear:
            return .controlClear(try controlClearPayload(object))
        }
    }

    // MARK: Relay order (growin-orders/1 O4)

    private static func relayPayload(_ object: [String: StrictJSONValue]) throws -> RelayOrderPayload {
        guard Set(object.keys) == relayTopKeys else {
            throw SignedPayloadInspectionError.invalidField("envelope keys")
        }
        let challengeId = try string(object, "challenge_id", pattern: "[A-Za-z0-9-]{8,64}")
        let nonce = try string(object, "nonce", pattern: "[A-Za-z0-9_-]{43}")
        let issuedAt = try integer(object, "issued_at")
        let expiresAt = try integer(object, "expires_at")
        guard issuedAt > 0, expiresAt > issuedAt else {
            throw SignedPayloadInspectionError.invalidField("expires_at")
        }
        let keyId = try string(object, "key_id", pattern: "[0-9a-f]{64}")
        let limits = try string(object, "limits_sha256", pattern: "[0-9a-f]{64}")
        guard case .object(let intentObject)? = object["intent"] else {
            throw SignedPayloadInspectionError.missingField("intent")
        }
        let intent = try relayIntent(intentObject)
        // The VM refuses a key or limits mismatch at mint, so honest bytes always agree.
        guard intent.keyId == keyId else {
            throw SignedPayloadInspectionError.invalidField("intent.key_id")
        }
        guard intent.limitsSha256 == limits else {
            throw SignedPayloadInspectionError.invalidField("intent.limits_sha256")
        }
        return RelayOrderPayload(
            challengeId: challengeId,
            nonce: nonce,
            issuedAt: issuedAt,
            expiresAt: expiresAt,
            keyId: keyId,
            limitsSha256: limits,
            intent: intent
        )
    }

    private static func relayIntent(_ object: [String: StrictJSONValue]) throws -> RelayOrderIntentFields {
        guard Set(object.keys) == relayIntentKeys else {
            throw SignedPayloadInspectionError.invalidField("intent keys")
        }
        let mode = try string(object, "mode")
        if mode == "LIVE" {
            throw SignedPayloadInspectionError.liveModeRefused
        }
        guard mode == "SHADOW" else {
            throw SignedPayloadInspectionError.invalidField("mode")
        }
        for (name, expected) in relayConstants {
            guard try string(object, name) == expected else {
                throw SignedPayloadInspectionError.invalidField(name)
            }
        }
        let side = try string(object, "side")
        guard side == "buy" || side == "sell" else {
            throw SignedPayloadInspectionError.invalidField("side")
        }
        let reason = try string(object, "reason")
        guard relayReasons.contains(reason) else {
            throw SignedPayloadInspectionError.invalidField("reason")
        }
        let quantity = try integer(object, "quantity")
        guard (1...100_000).contains(quantity) else {
            throw SignedPayloadInspectionError.invalidField("quantity")
        }
        let limit = try string(object, "limit_price", pattern: "[0-9]{1,7}(\\.[0-9]{1,2})?")
        guard let limitValue = Decimal(string: limit, locale: Locale(identifier: "en_US_POSIX")), limitValue > 0 else {
            throw SignedPayloadInspectionError.invalidField("limit_price")
        }
        var batchId: String?
        switch object["batch_id"] {
        case .null?:
            batchId = nil
        case .string(let value)?:
            guard matches(value, "[a-z0-9-]{8,64}") else {
                throw SignedPayloadInspectionError.invalidField("batch_id")
            }
            batchId = value
        default:
            throw SignedPayloadInspectionError.invalidField("batch_id")
        }
        return RelayOrderIntentFields(
            intentId: try string(object, "intent_id", pattern: "[A-Za-z0-9_-]{8,96}"),
            proposalId: try string(object, "proposal_id", pattern: "[A-Za-z0-9_-]{1,96}"),
            workspace: try string(object, "workspace"),
            broker: try string(object, "broker"),
            mode: mode,
            exchange: try string(object, "exchange"),
            product: try string(object, "product"),
            orderType: try string(object, "order_type"),
            validity: try string(object, "validity"),
            side: side,
            stockCode: try string(object, "stock_code", pattern: "[A-Z0-9]{1,10}"),
            isin: try string(object, "isin", pattern: "IN[A-Z0-9]{9}[0-9]"),
            quantity: quantity,
            limitPrice: limit,
            reason: reason,
            batchId: batchId,
            limitsSha256: try string(object, "limits_sha256", pattern: "[0-9a-f]{64}"),
            paramsSha256: try string(object, "params_sha256", pattern: "[0-9a-f]{64}"),
            keyId: try string(object, "key_id", pattern: "[0-9a-f]{64}")
        )
    }

    // MARK: Paper dispatch and control clear (backend/execution/approval.py)

    private static func paperPayload(_ object: [String: StrictJSONValue]) throws -> PaperDispatchPayload {
        PaperDispatchPayload(
            challengeId: try string(object, "challenge_id"),
            proposalId: try string(object, "proposal_id"),
            workspace: try string(object, "workspace"),
            mode: try string(object, "mode"),
            ticker: try string(object, "ticker", pattern: "[A-Za-z0-9.\\-_]{1,24}"),
            side: try string(object, "side", pattern: "[A-Za-z]{3,4}"),
            quantity: try string(object, "quantity", pattern: "[0-9]{1,12}(\\.[0-9]{1,8})?"),
            orderType: try optionalString(object, "order_type"),
            limitPrice: try optionalString(object, "limit_price"),
            keyId: try string(object, "key_id", pattern: "[0-9a-f]{64}"),
            expiresAt: try integer(object, "expires_at")
        )
    }

    private static func controlClearPayload(_ object: [String: StrictJSONValue]) throws -> ControlClearPayload {
        ControlClearPayload(
            challengeId: try string(object, "challenge_id"),
            workspace: try string(object, "workspace"),
            controlVersion: try integer(object, "control_version"),
            keyId: try string(object, "key_id", pattern: "[0-9a-f]{64}"),
            expiresAt: try integer(object, "expires_at")
        )
    }

    // MARK: Field helpers

    private static func string(
        _ object: [String: StrictJSONValue],
        _ name: String,
        pattern: String? = nil
    ) throws -> String {
        guard let value = object[name] else {
            throw SignedPayloadInspectionError.missingField(name)
        }
        guard case .string(let text) = value else {
            throw SignedPayloadInspectionError.invalidField(name)
        }
        if let pattern, !matches(text, pattern) {
            throw SignedPayloadInspectionError.invalidField(name)
        }
        return text
    }

    private static func optionalString(_ object: [String: StrictJSONValue], _ name: String) throws -> String? {
        switch object[name] {
        case nil, .null?:
            return nil
        case .string(let text)?:
            return text
        default:
            throw SignedPayloadInspectionError.invalidField(name)
        }
    }

    private static func integer(_ object: [String: StrictJSONValue], _ name: String) throws -> Int {
        guard let value = object[name] else {
            throw SignedPayloadInspectionError.missingField(name)
        }
        guard case .integer(let number) = value else {
            throw SignedPayloadInspectionError.invalidField(name)
        }
        return number
    }

    /// Whole-string match. `\z` (not `$`) so a trailing newline cannot slip through.
    private static func matches(_ text: String, _ pattern: String) -> Bool {
        text.range(of: "^(?:\(pattern))\\z", options: .regularExpression) != nil
    }
}

// MARK: - Strict JSON

nonisolated enum StrictJSONValue: Equatable, Sendable {
    case object([String: StrictJSONValue])
    case array([StrictJSONValue])
    case string(String)
    case integer(Int)
    case bool(Bool)
    case null
}

/// Canonical-ASCII JSON reader. Refuses what JSONSerialization quietly accepts.
nonisolated struct StrictJSONParser {
    static let maxBytes = 16 * 1024
    private static let maxDepth = 8

    private let bytes: [UInt8]
    private var index = 0

    static func parse(_ data: Data) throws -> StrictJSONValue {
        guard !data.isEmpty, data.count <= maxBytes else {
            throw SignedPayloadInspectionError.malformed
        }
        var parser = StrictJSONParser(bytes: [UInt8](data))
        let value = try parser.parseValue(depth: 0)
        guard parser.index == parser.bytes.count else {
            throw SignedPayloadInspectionError.trailingBytes
        }
        return value
    }

    private init(bytes: [UInt8]) {
        self.bytes = bytes
    }

    private var peek: UInt8? { index < bytes.count ? bytes[index] : nil }

    private mutating func parseValue(depth: Int) throws -> StrictJSONValue {
        guard depth <= Self.maxDepth, let byte = peek else {
            throw SignedPayloadInspectionError.malformed
        }
        switch byte {
        case UInt8(ascii: "{"):
            return try parseObject(depth: depth)
        case UInt8(ascii: "["):
            return try parseArray(depth: depth)
        case UInt8(ascii: "\""):
            return .string(try parseString())
        case UInt8(ascii: "t"):
            try expect("true")
            return .bool(true)
        case UInt8(ascii: "f"):
            try expect("false")
            return .bool(false)
        case UInt8(ascii: "n"):
            try expect("null")
            return .null
        case UInt8(ascii: "-"), UInt8(ascii: "0")...UInt8(ascii: "9"):
            return try parseNumber()
        default:
            throw SignedPayloadInspectionError.malformed
        }
    }

    private mutating func expect(_ literal: String) throws {
        let expected = Array(literal.utf8)
        guard index + expected.count <= bytes.count,
              Array(bytes[index..<index + expected.count]) == expected else {
            throw SignedPayloadInspectionError.malformed
        }
        index += expected.count
    }

    private mutating func parseObject(depth: Int) throws -> StrictJSONValue {
        index += 1  // {
        var result: [String: StrictJSONValue] = [:]
        if peek == UInt8(ascii: "}") {
            index += 1
            return .object(result)
        }
        while true {
            guard peek == UInt8(ascii: "\"") else {
                throw SignedPayloadInspectionError.malformed
            }
            let key = try parseString()
            guard peek == UInt8(ascii: ":") else {
                throw SignedPayloadInspectionError.malformed
            }
            index += 1
            let value = try parseValue(depth: depth + 1)
            guard result[key] == nil else {
                throw SignedPayloadInspectionError.duplicateKey
            }
            result[key] = value
            switch peek {
            case UInt8(ascii: ",")?:
                index += 1
            case UInt8(ascii: "}")?:
                index += 1
                return .object(result)
            default:
                throw SignedPayloadInspectionError.malformed
            }
        }
    }

    private mutating func parseArray(depth: Int) throws -> StrictJSONValue {
        index += 1  // [
        var result: [StrictJSONValue] = []
        if peek == UInt8(ascii: "]") {
            index += 1
            return .array(result)
        }
        while true {
            result.append(try parseValue(depth: depth + 1))
            switch peek {
            case UInt8(ascii: ",")?:
                index += 1
            case UInt8(ascii: "]")?:
                index += 1
                return .array(result)
            default:
                throw SignedPayloadInspectionError.malformed
            }
        }
    }

    private mutating func parseNumber() throws -> StrictJSONValue {
        let start = index
        if peek == UInt8(ascii: "-") { index += 1 }
        guard let first = peek, (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(first) else {
            throw SignedPayloadInspectionError.malformed
        }
        if first == UInt8(ascii: "0") {
            index += 1
        } else {
            while let byte = peek, (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(byte) { index += 1 }
        }
        if let next = peek, next == UInt8(ascii: ".") || next == UInt8(ascii: "e") || next == UInt8(ascii: "E") {
            throw SignedPayloadInspectionError.floatNotAllowed
        }
        if let next = peek, (UInt8(ascii: "0")...UInt8(ascii: "9")).contains(next) {
            throw SignedPayloadInspectionError.malformed  // leading zero
        }
        guard let text = String(bytes: bytes[start..<index], encoding: .ascii), let number = Int(text) else {
            throw SignedPayloadInspectionError.malformed
        }
        return .integer(number)
    }

    private mutating func parseString() throws -> String {
        index += 1  // opening quote
        var scalars = String.UnicodeScalarView()
        while true {
            guard let byte = peek else {
                throw SignedPayloadInspectionError.malformed
            }
            index += 1
            switch byte {
            case UInt8(ascii: "\""):
                return String(scalars)
            case UInt8(ascii: "\\"):
                scalars.append(try parseEscape())
            case 0x20..<0x7F:
                scalars.append(Unicode.Scalar(byte))
            default:
                // Control bytes and anything non-ASCII: the signed bytes are ASCII.
                throw SignedPayloadInspectionError.malformed
            }
        }
    }

    private mutating func parseEscape() throws -> Unicode.Scalar {
        guard let byte = peek else {
            throw SignedPayloadInspectionError.malformed
        }
        index += 1
        switch byte {
        case UInt8(ascii: "\""): return "\""
        case UInt8(ascii: "\\"): return "\\"
        case UInt8(ascii: "/"): return "/"
        case UInt8(ascii: "b"): return "\u{08}"
        case UInt8(ascii: "f"): return "\u{0C}"
        case UInt8(ascii: "n"): return "\n"
        case UInt8(ascii: "r"): return "\r"
        case UInt8(ascii: "t"): return "\t"
        case UInt8(ascii: "u"):
            let high = try parseHex4()
            if (0xD800...0xDBFF).contains(high) {
                guard peek == UInt8(ascii: "\\"), index + 1 < bytes.count, bytes[index + 1] == UInt8(ascii: "u") else {
                    throw SignedPayloadInspectionError.malformed
                }
                index += 2
                let low = try parseHex4()
                guard (0xDC00...0xDFFF).contains(low),
                      let scalar = Unicode.Scalar(0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)) else {
                    throw SignedPayloadInspectionError.malformed
                }
                return scalar
            }
            guard let scalar = Unicode.Scalar(high) else {
                throw SignedPayloadInspectionError.malformed
            }
            return scalar
        default:
            throw SignedPayloadInspectionError.malformed
        }
    }

    private mutating func parseHex4() throws -> UInt32 {
        guard index + 4 <= bytes.count,
              let text = String(bytes: bytes[index..<index + 4], encoding: .ascii),
              text.allSatisfy(\.isHexDigit),
              let value = UInt32(text, radix: 16) else {
            throw SignedPayloadInspectionError.malformed
        }
        index += 4
        return value
    }
}
