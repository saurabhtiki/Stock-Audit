def company_context(request):
    return {
        "current_company": getattr(request, "company", None),
        "user_companies": getattr(request, "companies", []),
        "current_role": getattr(request, "role", ""),
    }
