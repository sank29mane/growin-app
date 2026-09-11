import SwiftUI

struct PaperOperationsBlockingSlot: View {
    let reason: BlockingReason?

    var body: some View {
        let blocked = reason != nil
        HStack(alignment: .top, spacing: 8) {
            Rectangle()
                .fill(blocked ? Color.growinRed : Color.white.opacity(0.15))
                .frame(width: 4)

            Text(reason?.copy ?? PaperOperationsCopy.evidenceComplete)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                .foregroundStyle(blocked ? Color.growinRed : Color.brutalOffWhite)
                .lineLimit(nil)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minHeight: 48, alignment: .leading)
        .padding(16)
        .background(Color.brutalCharcoal)
        .border(Color.white.opacity(0.15), width: 1)
    }
}
