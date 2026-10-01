import SwiftUI

struct PaperOperationsHeaderView: View {
    var body: some View {
        HStack(alignment: .top, spacing: 16) {
            VStack(alignment: .leading, spacing: 8) {
                Text(PaperOperationsCopy.heading)
                    .font(SovereignTheme.Fonts.notoSerif(size: 24))
                    .foregroundStyle(Color.brutalOffWhite)

                Text(PaperOperationsCopy.subtitle)
                    .font(SovereignTheme.Fonts.spaceGrotesk(size: 16))
                    .foregroundStyle(Color.brutalChartreuse)
            }

            Spacer(minLength: 16)

            Text(PaperOperationsCopy.modeStrip)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 12, weight: .bold))
                .foregroundStyle(Color.brutalOffWhite)
                .multilineTextAlignment(.trailing)
        }
    }
}
