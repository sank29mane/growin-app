import SwiftUI

struct PaperOperationsBlockingSlot: View {
    let copy: String
    let blocked: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Rectangle()
                .fill(blocked ? Color.growinRed : Color.white.opacity(0.15))
                .frame(width: 4)

            Text(copy)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                .foregroundStyle(blocked ? Color.growinRed : Color.brutalOffWhite)
                .lineLimit(nil)
                .lineSpacing(8)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minHeight: 48, alignment: .leading)
        .padding(16)
        .background(Color.brutalCharcoal)
        .border(Color.white.opacity(0.15), width: 1)
    }
}
