import Foundation
import CoreML

/// SOTA 2026 Native ANE Inference Bridge
/// Handles loading and executing the NeuralJMCE model on the M4 NPU.
class JMCEInference {
    
    enum JMCEError: Error {
        case modelNotLoaded
        case predictionFailed
    }
    
    private var model: MLModel?
    
    init(modelName: String = "NeuralJMCE") {
        self.loadModel(named: modelName)
    }
    
    private func loadModel(named name: String) {
        let config = MLModelConfiguration()
        
        // SOTA: Target ANE primarily, fallback to GPU
        config.computeUnits = .all 
        
        // Optimization for M4 Pro: Allow high-precision 
        config.allowLowPrecisionAccumulationOnGPU = false
        
        do {
            // Load the model from the app bundle
            // Note: The model must be added to the Xcode project and exported as .mlmodel or .mlpackage
            if let modelURL = Bundle.main.url(forResource: name, withExtension: "mlmodelc") {
                self.model = try MLModel(contentsOf: modelURL, configuration: config)
                print("✅ JMCE Inference: Loaded on ANE/NPU")
            } else {
                print("⚠️ JMCE Inference: Model not found in bundle. Waiting for export.")
            }
        } catch {
            print("❌ JMCE Inference: Failed to load model: \(error)")
        }
    }
    
    /// Executes the forward pass on the NPU.
    /// - Parameter returns: Sequence of returns (SeqLen x N_Assets)
    /// - Returns: (mu, sigma, velocity)
    func predict(returns: MLMultiArray) throws -> (mu: [Double], sigma: [Double], velocity: [Double]?) {
        guard let model = self.model else {
            throw JMCEError.modelNotLoaded
        }
        
        // Wrap inputs
        let input = JMCEInput(returns: returns)
        
        do {
            let output = try model.prediction(from: input)
            
            // Extract outputs
            // mu: [N_Assets]
            // cholesky: [N_Assets * (N_Assets + 1) / 2]
            // velocity: [N_Assets * (N_Assets + 1) / 2]
            
            let mu = try extractDoubleArray(from: output.featureValue(for: "mu")?.multiArrayValue)
            let lFlat = try extractDoubleArray(from: output.featureValue(for: "cholesky")?.multiArrayValue)
            let vFlat = try? extractDoubleArray(from: output.featureValue(for: "velocity")?.multiArrayValue)
            
            // Reconstruct Sigma = L * L^T (Native Swift implementation)
            let sigma = reconstructSigma(from: lFlat)
            
            return (mu, sigma, vFlat)
        } catch {
            throw JMCEError.predictionFailed
        }
    }
    
    private func extractDoubleArray(from multiArray: MLMultiArray?) throws -> [Double] {
        guard let multiArray = multiArray else { return [] }
        var result = [Double](repeating: 0.0, count: multiArray.count)
        for i in 0..<multiArray.count {
            result[i] = multiArray[i].doubleValue
        }
        return result
    }
    
    /// Reconstructs the Covariance Matrix from the Cholesky factor.
    /// Uses native Swift loops (accelerated by AMX if possible).
    private func reconstructSigma(from lFlat: [Double]) -> [Double] {
        let n = Int(sqrt(Double(lFlat.count * 2))) // Solve N(N+1)/2 = count
        var sigma = [Double](repeating: 0.0, count: n * n)
        
        // 1. Build L matrix (Lower triangular)
        var lMatrix = [Double](repeating: 0.0, count: n * n)
        var k = 0
        for i in 0..<n {
            for j in 0...i {
                lMatrix[i * n + j] = lFlat[k]
                k += 1
            }
        }
        
        // 2. Compute Sigma = L * L^T
        // Use cblas_dgemm if available via Accelerate
        // For now, simple reconstruction for correctness
        for i in 0..<n {
            for j in 0..<n {
                var sum = 0.0
                for m in 0..<n {
                    sum += lMatrix[i * n + m] * lMatrix[j * n + m]
                }
                sigma[i * n + j] = sum
            }
        }
        
        return sigma
    }
}

/// Helper for CoreML Input mapping
class JMCEInput: MLFeatureProvider {
    var featureNames: Set<String> { ["returns"] }
    let returns: MLMultiArray
    
    init(returns: MLMultiArray) {
        self.returns = returns
    }
    
    func featureValue(for featureName: String) -> MLFeatureValue? {
        if featureName == "returns" {
            return MLFeatureValue(multiArray: returns)
        }
        return nil
    }
}
