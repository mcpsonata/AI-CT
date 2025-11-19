#!/usr/bin/env python3
"""
Debug script to identify where hierarchies are being lost in the evaluation process
"""

def debug_hierarchy_processing():
    """Debug the hierarchy evaluation process to find where hierarchies are lost"""
    
    print("🔍 Debugging Hierarchy Processing Loss")
    print("=" * 60)
    
    # Simulate the process with the known issue: 18 hierarchies found, 14 in output
    
    print("📊 **Known Issue:**")
    print("   - Expected hierarchies: 18")
    print("   - CSV output shows: 14")
    print("   - Missing hierarchies: 4")
    print()
    
    print("🔍 **Potential Loss Points in Processing Pipeline:**")
    print("-" * 50)
    
    # Analysis of where hierarchies could be lost
    potential_issues = [
        {
            "stage": "1. Hierarchy Discovery",
            "description": "tabular_editor.model.Tables.Hierarchies enumeration",
            "risk": "LOW",
            "reason": "Object model enumeration is usually reliable"
        },
        {
            "stage": "2. Level Extraction",
            "description": "Processing hierarchy.Levels to get column paths",
            "risk": "MEDIUM", 
            "reason": "Exception handling might skip hierarchies with complex levels"
        },
        {
            "stage": "3. AI Direction Analysis", 
            "description": "analyze_hierarchy_direction_with_ai() calls",
            "risk": "MEDIUM",
            "reason": "AI calls can fail, might skip hierarchies"
        },
        {
            "stage": "4. Batch Preparation",
            "description": "Splitting hierarchies into batches for AI processing",
            "risk": "LOW",
            "reason": "Simple list slicing, unlikely to lose items"
        },
        {
            "stage": "5. AI Batch Processing",
            "description": "analyze_hierarchies_with_ai() GPT-4o calls",
            "risk": "HIGH",
            "reason": "AI might not return all hierarchies if prompt is too long or parsing fails"
        },
        {
            "stage": "6. Result Processing", 
            "description": "Processing ai_results into final entries",
            "risk": "MEDIUM",
            "reason": "Exception handling might skip problematic hierarchies"
        },
        {
            "stage": "7. CSV Generation",
            "description": "Converting analysis_results to CSV format", 
            "risk": "LOW",
            "reason": "Simple DataFrame operations"
        }
    ]
    
    for issue in potential_issues:
        risk_emoji = {"LOW": "🟢", "MEDIUM": "🟡", "HIGH": "🔴"}[issue["risk"]]
        print(f"{risk_emoji} **{issue['stage']}**")
        print(f"   Risk Level: {issue['risk']}")
        print(f"   Process: {issue['description']}")
        print(f"   Analysis: {issue['reason']}")
        print()
    
    print("🎯 **Most Likely Causes (High/Medium Risk):**")
    print("-" * 40)
    print()
    
    print("🔴 **AI Batch Processing Issues:**")
    print("   - GPT-4o token limit exceeded causing truncated responses")
    print("   - JSON parsing errors dropping some hierarchy results")
    print("   - AI model returning fewer results than input hierarchies")
    print("   - Prompt complexity causing AI to skip some items")
    print()
    
    print("🟡 **Exception Handling Issues:**")
    print("   - try/catch blocks swallowing failed hierarchies")
    print("   - Level extraction failures causing hierarchy skip")
    print("   - Direction analysis failures causing hierarchy drop")
    print()
    
    print("🔧 **Debugging Strategy:**")
    print("-" * 25)
    print("1. **Add comprehensive logging** at each processing stage")
    print("2. **Track hierarchy counts** before/after each stage")
    print("3. **Log input vs output** for AI batch processing")
    print("4. **Check exception logs** for swallowed errors")
    print("5. **Verify AI response completeness** in GPT-4o calls")
    print()
    
    print("📋 **Specific Debug Points to Add:**")
    print("   ✓ Hierarchy discovery count logging")
    print("   ✓ Hierarchy data preparation tracking")  
    print("   ✓ AI batch input/output comparison")
    print("   ✓ Final result count verification")
    print("   ✓ Missing hierarchy identification")
    print()
    
    print("🎯 **Expected Debug Output Pattern:**")
    print("-" * 35)
    print("📊 Initial hierarchies discovered: 18")
    print("📊 Hierarchies prepared for AI: 18")
    print("📊 Total hierarchies processed: 18")
    print("📊 Final analysis results: 18")
    print("✅ FINAL RESULT COUNT OK: All 18 hierarchies processed")
    print()
    print("**OR (if issue found):**")
    print("❌ BATCH PROCESSING LOSS: Sent 18, got 14 results")
    print("❌ Missing hierarchies in batch processing!")
    print("❌ LOST: Account.Hierarchy1, Product.Hierarchy2, etc.")

if __name__ == "__main__":
    debug_hierarchy_processing()