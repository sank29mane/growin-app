import SwiftUI

struct AccountPicker: View {
    @Binding var selectedAccount: String
    private let accounts = ["All", "ISA", "Invest"]
    
    var body: some View {
        HStack(spacing: 8) {
            ForEach(accounts, id: \.self) { account in
                let isSelected = selectedAccount == (account == "All" ? "all" : account.lowercased())
                
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
                    .background(isSelected ? Color.cyan : Color.white.opacity(0.05))
                    .foregroundStyle(isSelected ? Color.black : Color.brutalOffWhite)
                    .clipShape(Capsule())
                    .overlay(
                        Capsule()
                            .stroke(isSelected ? Color.clear : Color.white.opacity(0.1), lineWidth: 1)
                    )
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(account) Account")
                .accessibilityAddTraits(isSelected ? [.isSelected, .isButton] : [.isButton])
            }
        }
        .padding(.vertical, 8)
    }
}

#Preview {
    ZStack {
        Color.black.ignoresSafeArea()
        AccountPicker(selectedAccount: .constant("all"))
    }
}
