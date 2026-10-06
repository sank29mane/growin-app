import Foundation
import Testing
@testable import Growin

/// The app shows model roles from `GET /api/models/roles` and sends no model,
/// provider or key of its own (Phase 67). The fixture below is synthetic.
struct ModelRolesTests {
    private static let fixture = """
    {
      "roles": [
        {"role": "coordinator", "provider": "lmstudio", "kind": "openai_compatible",
         "model": "stub-coordinator-model", "key_configured": null},
        {"role": "decision", "provider": "xai", "kind": "openai_compatible",
         "model": "stub-decision-model", "key_configured": true},
        {"role": "risk_critic", "provider": "xai", "kind": "openai_compatible",
         "model": "stub-risk-model", "key_configured": false},
        {"role": "forecaster", "provider": "local_checkpoint", "kind": "hf_local",
         "model": "stub-org/stub-forecaster", "key_configured": null}
      ],
      "missing_roles": ["research", "math_codegen"]
    }
    """

    @Test func decodesTheRolesFixture() throws {
        let decoded = try JSONDecoder().decode(
            ModelRolesResponse.self,
            from: Data(Self.fixture.utf8)
        )

        #expect(decoded.roles.count == 4)
        #expect(decoded.missingRoles == ["research", "math_codegen"])

        let decision = try #require(decoded.roles.first { $0.role == "decision" })
        #expect(decision.id == "decision")
        #expect(decision.provider == "xai")
        #expect(decision.kind == "openai_compatible")
        #expect(decision.model == "stub-decision-model")
        #expect(decision.keyConfigured == true)

        // null means the provider takes no key; false means its key variable is unset.
        #expect(decoded.roles[0].keyConfigured == nil)
        #expect(decoded.roles[2].keyConfigured == false)
    }

    @Test func rolesResponseCarriesNoUrlOrKeyFields() throws {
        let encoded = try JSONEncoder().encode(
            try JSONDecoder().decode(ModelRolesResponse.self, from: Data(Self.fixture.utf8))
        )
        let text = String(decoding: encoded, as: UTF8.self)
        #expect(!text.contains("base_url"))
        #expect(!text.contains("api_key_env"))
        #expect(!text.contains("http"))
    }

    @Test func chatRequestBodyCarriesNoModelProviderOrKey() throws {
        let body = GrowinChatMessage(
            message: "hello",
            conversationId: nil,
            accountType: "invest",
            images: nil
        )
        let object = try #require(
            try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        )
        #expect(object["message"] as? String == "hello")
        #expect(object["model_name"] == nil)
        #expect(object["coordinator_model"] == nil)
        #expect(object["api_keys"] == nil)
    }
}
