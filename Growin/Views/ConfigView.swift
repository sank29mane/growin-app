import SwiftUI

struct ConfigView: View {
    @Environment(\.dismiss) var dismiss
    @AppStorage("openaiApiKey") private var openaiApiKey = ""
    @AppStorage("geminiApiKey") private var geminiApiKey = ""
    @AppStorage("trading212ApiKey") private var trading212ApiKey = "" // Added as requested for "Configure MCP"
    
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
                    }
                    
                    VStack(alignment: .leading) {
                        Text("Gemini API Key")
                            .font(.caption)
                        SecureField("AIza...", text: $geminiApiKey)
                            .textFieldStyle(.roundedBorder)
                    }
                }
                
                Section("Trading 212 MCP") {
                    VStack(alignment: .leading) {
                        Text("API Key")
                            .font(.caption)
                        SecureField("Your T212 API Key", text: $trading212ApiKey)
                            .textFieldStyle(.roundedBorder)
                    }
                    
                    Text("Required for Portfolio Analysis and Trading operations.")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
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
                }
            }
        }
        .frame(width: 400, height: 500)
    }
}

#Preview {
    ConfigView(provider: "OpenAI")
}
