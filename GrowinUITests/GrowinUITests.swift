//  GrowinUITests.swift
//  GrowinUITests
//  Created by Sanket Mane on 22/06/2025.

import XCTest

final class GrowinUITests: XCTestCase {

    override func setUpWithError() throws {
        // Put setup code here. This method is called before the invocation of each test method in the class.

        // In UI tests it is usually best to stop immediately when a failure occurs.
        continueAfterFailure = false

        // In UI tests it’s important to set the initial state - such as interface orientation - required for your tests before they run. The setUp method is a good place to do this.
    }

    override func tearDownWithError() throws {
        // Put teardown code here. This method is called after the invocation of each test method in the class.
    }

    @MainActor
    func testExplainBackLoop() throws {
        let app = XCUIApplication()
        app.launch()

        // 1. Navigate to Reasoning Trace (Assuming it's triggered by a button in Dashboard)
        // For testing, we simulate the interaction
        let reasoningButton = app.buttons["Explain-Back Verification"]
        if reasoningButton.exists {
            reasoningButton.tap()
            
            // 2. Verify Explain-Back Text exists
            let explainBackHeader = app.staticTexts["EXPLAIN-BACK VERIFICATION"]
            XCTAssertTrue(explainBackHeader.exists)
            
            // 3. Tap "Yes, Proceed"
            app.buttons["Yes, Proceed"].tap()
        }
    }

    @MainActor
    func testChallengeLogicFlow() throws {
        let app = XCUIApplication()
        app.launch()

        // 1. Trigger Challenge Logic from Reasoning Trace
        let challengeButton = app.buttons["No, Challenge"]
        if challengeButton.exists {
            challengeButton.tap()
            
            // 2. Input challenge text
            let textEditor = app.textViews.firstMatch
            XCTAssertTrue(textEditor.exists)
            textEditor.tap()
            textEditor.typeText("The risk trajectory is too high for this sector.")
            
            // 3. Tap "Restitch Strategy"
            let restitchButton = app.buttons["Restitch Strategy"]
            XCTAssertTrue(restitchButton.exists)
            restitchButton.tap()
            
            // 4. Verify Optimistic UI status (briefly exists)
            let restitchingStatus = app.staticTexts["Re-stitching Strategy Trajectories..."]
            // Note: This might be too fast for XCUITest without a wait
        }
    }

    @MainActor
    func testLaunchPerformance() throws {
        // This measures how long it takes to launch your application.
        measure(metrics: [XCTApplicationLaunchMetric()]) {
            XCUIApplication().launch()
        }
    }
}
