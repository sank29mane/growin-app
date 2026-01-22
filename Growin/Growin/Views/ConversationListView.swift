import Combine
import SwiftUI

struct ConversationItem: Codable, Identifiable {
    let id: String
    let createdAt: String
    let title: String?
    var lastMessage: String?

    enum CodingKeys: String, CodingKey {
        case id
        case createdAt = "created_at"
        case title
        case lastMessage = "last_message"
    }
}

class ConversationListViewModel: ObservableObject {
    @Published var conversations: [ConversationItem] = []
    @Published var isLoading = false
    @Published var errorMessage: String?

    private let baseURL = "http://127.0.0.1:8002"
    private var cancellables = Set<AnyCancellable>()

    init() {
        NotificationCenter.default.publisher(for: NSNotification.Name("RefreshConversations"))
            .sink { [weak self] _ in
                Task {
                    await self?.fetchConversations()
                }
            }
            .store(in: &cancellables)
    }

    @MainActor
    func fetchConversations() async {
        isLoading = true
        defer { isLoading = false }

        guard let url = URL(string: "\(baseURL)/conversations") else { return }

        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            let decoded = try JSONDecoder().decode([ConversationItem].self, from: data)

            conversations = decoded
            errorMessage = nil
        } catch {
            errorMessage = error.localizedDescription
            print("Failed to fetch conversations: \(error)")
        }
    }

    @MainActor
    func deleteConversations(ids: [String]) async {
        isLoading = true
        defer { isLoading = false }

        for id in ids {
            guard let url = URL(string: "\(baseURL)/conversations/\(id)") else { continue }
            var request = URLRequest(url: url)
            request.httpMethod = "DELETE"

            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                if let httpResponse = response as? HTTPURLResponse, httpResponse.statusCode == 200 {
                    print("Deleted conversation: \(id)")
                } else {
                    let errorBody = String(data: data, encoding: .utf8) ?? "no body"
                    print("Failed to delete conversation \(id): \(errorBody)")
                }
            } catch {
                print("Error deleting conversation \(id): \(error)")
            }
        }

        await fetchConversations()
    }
}

struct ConversationListView: View {
    @StateObject private var viewModel = ConversationListViewModel()
    @Binding var selectedConversationId: String?
    @Environment(\.dismiss) private var dismiss

    @State private var isEditing = false
    @State private var selectedIds: Set<String> = []
    @State private var showDeleteConfirmation = false

    var selectAllButtonTitle: String {
        selectedIds.count == viewModel.conversations.count ? "Deselect All" : "Select All"
    }

    func toggleSelectAll() {
        if selectedIds.count == viewModel.conversations.count {
            selectedIds.removeAll()
        } else {
            selectedIds = Set(viewModel.conversations.map { $0.id })
        }
    }

    var body: some View {
        NavigationStack {
            ZStack {
                Color.black.ignoresSafeArea()

                if viewModel.isLoading && viewModel.conversations.isEmpty {
                    ProgressView()
                        .tint(.white)
                } else if viewModel.conversations.isEmpty {
                    ContentUnavailableView {
                        Label("No Conversations", systemImage: "bubble.left.and.bubble.right")
                    } description: {
                        Text("Start chatting to see your conversation history here")
                    }
                } else {
                    ScrollView {
                        LazyVStack(spacing: 12) {
                            ForEach(viewModel.conversations) { conversation in
                                ConversationCard(
                                    conversation: conversation,
                                    isSelected: selectedIds.contains(conversation.id),
                                    isEditing: isEditing
                                ) {
                                    if isEditing {
                                        if selectedIds.contains(conversation.id) {
                                            selectedIds.remove(conversation.id)
                                        } else {
                                            selectedIds.insert(conversation.id)
                                        }
                                    } else {
                                        selectedConversationId = conversation.id
                                        dismiss()
                                    }
                                }
                            }
                        }
                        .padding()
                    }
                    .refreshable {
                        await viewModel.fetchConversations()
                    }
                }
            }
            .navigationTitle("Conversations")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(action: { dismiss() }) {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.gray.opacity(0.8))
                    }
                }

                ToolbarItem {
                    if isEditing {
                        Button("Cancel") {
                            isEditing = false
                            selectedIds.removeAll()
                        }
                        .foregroundStyle(.white)
                    } else {
                        Button(action: {
                            selectedConversationId = nil
                            dismiss()
                        }) {
                            Image(systemName: "plus")
                        }
                    }
                }

                ToolbarItem {
                    HStack {
                        if isEditing {
                            // Select All / Deselect All button
                            Button(selectAllButtonTitle) {
                                toggleSelectAll()
                            }
                            .foregroundStyle(.white)

                            // Delete All button
                            if !selectedIds.isEmpty {
                                Button(role: .destructive) {
                                    showDeleteConfirmation = true
                                } label: {
                                    HStack {
                                        Image(systemName: "trash")
                                        Text("Delete All (\(selectedIds.count))")
                                    }
                                    .foregroundStyle(.red)
                                }
                            }
                        }

                        Button(isEditing ? "Done" : "Edit") {
                            isEditing.toggle()
                            if !isEditing {
                                selectedIds.removeAll()
                            }
                        }
                        .foregroundStyle(.white)
                    }
                }
            }
            .task {
                await viewModel.fetchConversations()
            }
            .alert("Delete \(selectedIds.count) Conversation\(selectedIds.count == 1 ? "" : "s")?", isPresented: $showDeleteConfirmation) {
                Button("Cancel", role: .cancel) { }
                Button("Delete", role: .destructive) {
                    Task {
                        await viewModel.deleteConversations(ids: Array(selectedIds))
                        isEditing = false
                        selectedIds.removeAll()
                        showDeleteConfirmation = false
                    }
                }
            } message: {
                Text("This action cannot be undone. All selected conversations will be permanently deleted.")
            }
        }
    }
}

struct ConversationCard: View {
    let conversation: ConversationItem
    let isSelected: Bool
    let isEditing: Bool
    let onTap: () -> Void

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 12) {
                if isEditing {
                    Image(systemName: isSelected ? "checkmark.circle.fill" : "circle")
                        .foregroundStyle(isSelected ? Color.blue : Color.secondary)
                        .font(.system(size: 20))
                }

                GlassCard(cornerRadius: 12) {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack {
                            Text(conversation.title ?? "Untitled Conversation")
                                .font(.system(size: 14, weight: .bold))
                                .foregroundStyle(.white)
                                .lineLimit(1)

                            Spacer()

                            Text(formatDate(conversation.createdAt))
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                        }

                        if let preview = conversation.lastMessage {
                            Text(preview)
                                .font(.system(size: 12))
                                .foregroundStyle(.white.opacity(0.7))
                                .lineLimit(2)
                        }
                    }
                    .padding(12)
                }
            }
        }
        .buttonStyle(.plain)
    }

    private func formatDate(_ dateString: String) -> String {
        let formatter = ISO8601DateFormatter()
        if let date = formatter.date(from: dateString) {
            let relativeFormatter = RelativeDateTimeFormatter()
            relativeFormatter.unitsStyle = .abbreviated
            return relativeFormatter.localizedString(for: date, relativeTo: Date())
        }
        return dateString
    }
}

#Preview {
    ConversationListView(selectedConversationId: .constant(nil))
}
