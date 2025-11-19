#!/usr/bin/env python3
"""
Test complete integration of level-specific analysis in hierarchy evaluation
"""

def test_complete_integration():
    """Test how level-specific analysis integrates with the full scoring system"""
    
    print("🧪 Testing Complete Integration: Level-Specific Analysis in Score Rationale")
    print("=" * 80)
    
    # Simulate the hierarchy evaluation process for non-Microsoft patterns
    test_scenarios = [
        {
            "hierarchy_name": "Poor Custom Sales",
            "table_name": "Sales",
            "column_path": "Level1 > Level2 > TeamID > PersonKey",
            "table_type": "DIMENSION",
            "level_count": 4,
            "hierarchy_direction": "High to Low",
            "simulated_ai_hq_002": 0.2  # Low AI score due to poor structure
        },
        {
            "hierarchy_name": "Good Custom Organization", 
            "table_name": "Organization",
            "column_path": "Region > Territory > District > Store",
            "table_type": "DIMENSION", 
            "level_count": 4,
            "hierarchy_direction": "High to Low",
            "simulated_ai_hq_002": 0.35  # Better AI score for good structure
        },
        {
            "hierarchy_name": "Terrible Abbreviations",
            "table_name": "Product",
            "column_path": "DIV > CAT > ITM",
            "table_type": "DIMENSION",
            "level_count": 3,
            "hierarchy_direction": "Unknown", 
            "simulated_ai_hq_002": 0.1  # Very low score
        }
    ]
    
    print("📊 **Integration Test Scenarios:**")
    print("-" * 50)
    
    for i, scenario in enumerate(test_scenarios, 1):
        hierarchy_name = scenario["hierarchy_name"]
        table_name = scenario["table_name"]
        column_path = scenario["column_path"]
        table_type = scenario["table_type"]
        level_count = scenario["level_count"]
        hierarchy_direction = scenario["hierarchy_direction"]
        simulated_ai_hq_002 = scenario["simulated_ai_hq_002"]
        
        print(f"\n📋 **Scenario {i}: {hierarchy_name}**")
        print(f"   Table: {table_name} ({table_type})")
        print(f"   Path: {column_path}")
        print(f"   Direction: {hierarchy_direction}")
        print(f"   AI HQ-002 Score: {simulated_ai_hq_002}")
        
        # Simulate the Microsoft pattern detection (should be "Unknown" for these tests)
        ms_pattern_detected = "Unknown"
        microsoft_base_score = 0.0
        
        # Calculate scores
        hq_001_score = 0.4 if table_type.upper() in ["DIMENSION", "DIM"] else 0.0
        hq_002_score = simulated_ai_hq_002  # Use AI score since no Microsoft pattern
        calculated_total_score = hq_001_score + hq_002_score
        
        # Simulate level-specific analysis
        level_specific_recommendations = []
        pattern_recommendations = []
        level_recommendations = []
        direction_recommendations = []
        
        if ms_pattern_detected == "Unknown" and hq_002_score < 0.6:
            # Parse column path into individual levels
            if column_path and column_path != "Column details unavailable":
                levels = [level.strip() for level in column_path.split(" > ")]
                
                # Analyze each level for issues
                for j, level in enumerate(levels):
                    level_number = j + 1
                    level_clean = level.lower().replace(" ", "").replace("_", "").replace("-", "")
                    
                    # Check for generic/poor naming patterns
                    if any(generic in level_clean for generic in ["level", "node", "item", "field", "data", "value"]):
                        level_specific_recommendations.append(f"Level {level_number} ('{level}') uses generic naming - replace with business-meaningful term")
                    
                    # Check for technical naming patterns  
                    elif any(tech in level_clean for tech in ["id", "key", "code", "ref", "num"]):
                        level_specific_recommendations.append(f"Level {level_number} ('{level}') appears technical - consider business-friendly name")
                    
                    # Check for unclear abbreviations
                    elif len(level.replace(" ", "")) <= 3 and level.isupper():
                        level_specific_recommendations.append(f"Level {level_number} ('{level}') abbreviation unclear - spell out full business term")
                
                # Overall structure analysis
                if len(levels) > 7:
                    level_specific_recommendations.append(f"Too many levels ({len(levels)}) - consider consolidating to 4-6 levels for better usability")
                elif len(levels) < 3:
                    level_specific_recommendations.append(f"Too few levels ({len(levels)}) - consider adding intermediate levels for better drill-down analysis")
                
                # Add level-specific recommendations to pattern recommendations
                if level_specific_recommendations:
                    pattern_recommendations.extend(level_specific_recommendations[:3])  # Top 3
        
        # Add general pattern analysis
        if hq_002_score < 0.3:
            if ms_pattern_detected == "Unknown":
                pattern_recommendations.append("Hierarchy doesn't follow standard Microsoft BI patterns - consider restructuring based on level analysis above")
            else:
                pattern_recommendations.append("Hierarchy doesn't follow standard Microsoft BI patterns - consider restructuring")
        elif hq_002_score < 0.5:
            if ms_pattern_detected == "Unknown":
                pattern_recommendations.append("Hierarchy partially follows business logic but needs optimization based on level analysis")
            else:
                pattern_recommendations.append("Hierarchy partially follows Microsoft standards but needs optimization")
        
        # Direction recommendations
        if hierarchy_direction == "Unknown":
            direction_recommendations.append("Define clear hierarchy direction for optimal user navigation")
        
        # Generate score rationale with level-specific analysis
        if table_type.upper() in ["DIMENSION", "DIM"]:
            if hq_001_score >= 0.4:
                if calculated_total_score >= 0.8:
                    if ms_pattern_detected != "Unknown":
                        score_rationale = f"Excellent: Hierarchy properly placed in Dimension table with high quality {ms_pattern_detected.lower()} pattern"
                    else:
                        score_rationale = f"Excellent: Well-structured custom hierarchy in Dimension table with clear business logic"
                elif calculated_total_score >= 0.6:
                    # Build specific recommendations including level-specific analysis
                    recommendations = []
                    recommendations.extend(pattern_recommendations)
                    recommendations.extend(level_recommendations)
                    recommendations.extend(direction_recommendations)
                    
                    if recommendations:
                        rec_text = "; ".join(recommendations[:2])  # Limit to top 2 recommendations
                        if ms_pattern_detected != "Unknown":
                            score_rationale = f"Good: Dimension table placement correct. Improvements: {rec_text}"
                        else:
                            score_rationale = f"Good: Custom hierarchy in Dimension table. Level analysis: {rec_text}"
                    else:
                        if ms_pattern_detected != "Unknown":
                            score_rationale = f"Good: Hierarchy appropriately placed in Dimension table following {ms_pattern_detected.lower()} pattern"
                        else:
                            score_rationale = f"Good: Custom hierarchy appropriately placed in Dimension table"
                else:
                    # Lower scores need more specific guidance with level analysis
                    recommendations = []
                    recommendations.extend(pattern_recommendations)
                    recommendations.extend(level_recommendations)
                    
                    if recommendations:
                        rec_text = "; ".join(recommendations[:2])  # Show top 2 recommendations
                        if ms_pattern_detected != "Unknown":
                            score_rationale = f"Fair: Dimension table placement correct but needs pattern optimization. Priority: {rec_text}"
                        else:
                            score_rationale = f"Fair: Custom hierarchy needs improvement. Key issues: {rec_text}"
                    else:
                        score_rationale = "The hierarchy is placed in a Dimension table but could benefit from business pattern optimization"
        
        # Calculate severity
        score_percentage = calculated_total_score / 1.0
        if score_percentage < 0.60:
            issue_severity = "critical"
        elif 0.60 <= score_percentage <= 0.74:
            issue_severity = "issue"
        elif 0.75 <= score_percentage <= 0.84:
            issue_severity = "warning"
        else:
            issue_severity = "good"
        
        # Calculate category
        if calculated_total_score >= 0.8:
            score_category = "Excellent"
        elif calculated_total_score >= 0.6:
            score_category = "Good" 
        elif calculated_total_score >= 0.4:
            score_category = "Fair"
        else:
            score_category = "Poor"
        
        # Display complete results
        print(f"\n   📊 **Complete Scoring Results:**")
        print(f"      HQ-001 (Table Alignment): {hq_001_score:.1f}/0.4")
        print(f"      HQ-002 (Business Pattern): {hq_002_score:.1f}/0.6")
        print(f"      Total Score: {calculated_total_score:.1f}/1.0")
        print(f"      Category: {score_category}")
        print(f"      Severity: {issue_severity.upper()}")
        
        print(f"\n   🔍 **Level-Specific Issues Found:**")
        if level_specific_recommendations:
            for rec in level_specific_recommendations[:5]:  # Show up to 5
                print(f"      • {rec}")
        else:
            print(f"      ✅ No specific level issues detected")
        
        print(f"\n   📝 **Final Score Rationale:**")
        print(f"      {score_rationale}")
        
        print(f"\n   {'='*60}")
    
    print(f"\n{'='*80}")
    print("📈 **Integration Benefits Summary:**")
    print()
    print("🎯 **Enhanced Non-Microsoft Pattern Analysis:**")
    print("   • Specific level numbers identified for targeted improvements")
    print("   • Actionable recommendations for each problematic level")
    print("   • Integration with overall business pattern scoring")
    print()
    print("📊 **Score Rationale Enhancement:**")
    print("   • Level-specific issues included in recommendations")  
    print("   • Clear distinction between Microsoft vs. custom patterns")
    print("   • Prioritized improvement suggestions (top 2-3 items)")
    print()
    print("🔧 **Practical Value:**")
    print("   • Users know exactly which levels to fix")
    print("   • Specific naming and structure guidance provided")
    print("   • Business logic validation beyond generic pattern matching")
    print()
    print("⚖️ **Fair Scoring Approach:**")
    print("   • Microsoft patterns: guaranteed 0.6 score")
    print("   • Good custom patterns: 0.3-0.45 with specific guidance")
    print("   • Poor custom patterns: 0.0-0.2 with detailed improvement path")

if __name__ == "__main__":
    test_complete_integration()