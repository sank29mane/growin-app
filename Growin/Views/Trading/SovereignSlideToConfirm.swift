import SwiftUI

#if os(macOS)
import AppKit
#endif

/// SovereignSlideToConfirm: A professional macOS native safety mechanism.
/// Requires a high-intent drag to confirm critical trade execution.
struct SovereignSlideToConfirm: View {
    @Binding var isConfirmed: Bool
    var title: String = "SLIDE TO COMMIT EXECUTION"
    var onConfirm: () -> Void
    
    @State private var dragOffset: CGFloat = 0
    @State private var isDragging: Bool = false
    private let trackWidth: CGFloat = 352 // 400 total - 48 padding
    private let handleSize: CGFloat = 50
    
    var body: some View {
        ZStack(alignment: .leading) {
            // Track
            Rectangle()
                .fill(Color.black.opacity(0.4))
                .frame(height: handleSize)
                .border(SovereignTheme.Colors.technicalBorder, width: 1)
            
            // Text Layer
            Text(title)
                .font(SovereignTheme.Fonts.spaceGrotesk(size: 10, weight: .bold))
                .foregroundStyle(Color.brutalOffWhite.opacity(0.3))
                .frame(maxWidth: .infinity)
                .multilineTextAlignment(.center)
            
            // Handle
            Rectangle()
                .fill(Color.brutalChartreuse)
                .frame(width: handleSize, height: handleSize)
                .overlay(
                    Image(systemName: "chevron.right.2")
                        .font(.system(size: 14, weight: .black))
                        .foregroundStyle(Color.black)
                )
                .offset(x: dragOffset)
                .gesture(
                    DragGesture()
                        .onChanged { value in
                            isDragging = true
                            let newOffset = value.translation.width
                            if newOffset >= 0 && newOffset <= trackWidth - handleSize {
                                dragOffset = newOffset
                            }
                        }
                        .onEnded { value in
                            isDragging = false
                            if dragOffset >= trackWidth - handleSize - 5 {
                                // Threshold met
                                #if os(macOS)
                                NSHapticFeedbackManager.defaultPerformer.perform(.generic, performanceTime: .now)
                                #endif
                                withAnimation(.spring(response: 0.3, dampingFraction: 0.6)) {
                                    dragOffset = trackWidth - handleSize
                                }
                                isConfirmed = true
                                onConfirm()
                            } else {
                                // Reset
                                withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                                    dragOffset = 0
                                }
                            }
                        }
                )
        }
        .frame(width: trackWidth, height: handleSize)
        .contentShape(Rectangle())
    }
}

#Preview {
    VStack {
        SovereignSlideToConfirm(isConfirmed: .constant(false)) {
            print("Confirmed")
        }
    }
    .padding()
    .background(Color.brutalRecessed)
}
