import SwiftUI
#if os(macOS)
import AppKit
#endif

struct AccountPicker: View {
    @Binding var selectedAccount: String
    private let accounts = ["All", "ISA", "Invest"]
    
    var body: some View {
        HStack(spacing: 8) {
            ForEach(accounts, id: \.self) { account in
                AccountPickerButton(account: account, selectedAccount: $selectedAccount)
            }
        }
        .padding(.vertical, 8)
    }
}

private struct AccountPickerButton: View {
    let account: String
    @Binding var selectedAccount: String
    @State private var isHovered = false

    var isSelected: Bool {
        selectedAccount == (account == "All" ? "all" : account.lowercased())
    }

    var body: some View {
        Button(action: {
            withAnimation(.spring(response: 0.3, dampingFraction: 0.7)) {
                selectedAccount = account == "All" ? "all" : account.lowercased()
            }
        }) {
            HStack(spacing: 6) {
                if account == "All" {
                    Image(systemName: "brain.filled.head.profile")
                        .font(.system(size: 10))
                } else if account == "ISA" {
                    Image(systemName: "star.fill")
                        .font(.system(size: 10))
                } else {
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.system(size: 10))
                }

                Text(account)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 11, weight: .bold))
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 8)
            .background(isSelected ? Color.cyan : (isHovered ? Color.white.opacity(0.1) : Color.white.opacity(0.05)))
            .foregroundStyle(isSelected ? Color.black : (isHovered ? Color.white : Color.brutalOffWhite))
            .clipShape(Capsule())
            .overlay(
                Capsule()
                    .stroke(isSelected ? Color.clear : (isHovered ? Color.white.opacity(0.3) : Color.white.opacity(0.1)), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(account) Account")
        .accessibilityHint("Switches current context to \(account) account")
        .accessibilityAddTraits(isSelected ? [.isSelected, .isButton] : [.isButton])
        .onHover { hovering in
            withAnimation(.easeInOut(duration: 0.15)) {
                isHovered = hovering
            }
            #if os(macOS)
            if hovering {
                NSCursor.pointingHand.push()
            } else {
                NSCursor.pop()
            }
            #endif
        }
        .onDisappear {
            #if os(macOS)
            if isHovered {
                NSCursor.pop()
            }
            #endif
        }
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        AccountPicker(selectedAccount: .constant("all"))
    }
}
