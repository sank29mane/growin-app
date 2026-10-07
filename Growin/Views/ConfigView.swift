import SwiftUI

struct ConfigView: View {
    @Environment(\.dismiss) var dismiss
    @KeychainStorage(.openaiApiKey, scope: .shared) private var openaiApiKey = ""
    @KeychainStorage(.geminiApiKey, scope: .shared) private var geminiApiKey = ""
    
    var provider: String? // Optional provider that triggered this
    
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    Text("To use \(provider ?? "AI Models") properly, please configure the required API keys. These are stored securely on your device.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                
                Section("OpenAI & Gemini") {
                    VStack(alignment: .leading) {
                        Text("OpenAI API Key")
                            .font(.caption)
                        SecureField("sk-...", text: $openaiApiKey)
                            .textFieldStyle(.roundedBorder)
                            .accessibilityLabel("OpenAI API Key")
                            .accessibilityHint("Enter your OpenAI API key")
                    }
                    
                    VStack(alignment: .leading) {
                        Text("Gemini API Key")
                            .font(.caption)
                        SecureField("AIza...", text: $geminiApiKey)
                            .textFieldStyle(.roundedBorder)
                            .accessibilityLabel("Gemini API Key")
                            .accessibilityHint("Enter your Gemini API key")
                    }
                }
                
                Section {
                    Button("Save and Continue") {
                        dismiss()
                    }
                    .frame(maxWidth: .infinity)
                    .buttonStyle(.borderedProminent)
                }
            }
            .navigationTitle("Configuration Needed")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                        .accessibilityLabel("Cancel configuration")
                        .accessibilityHint("Dismisses the configuration view without saving")
                        .accessibilityAddTraits(.isButton)
                }
            }
        }
        .frame(width: 400, height: 500)
    }
}

#Preview {
    ConfigView(provider: "OpenAI")
}
