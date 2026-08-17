def test_package_importable():
    import llmsearch
    assert llmsearch.__version__ == "0.1.0"
