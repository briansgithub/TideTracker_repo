$(function () {
    var networks = undefined;

    function showHideFormFields() {
        var security = $(this).find(':selected').attr('data-security');
        // start off with all fields hidden
        $('#identity-group').addClass('hidden');
        $('#passphrase-group').addClass('hidden');
        $('#hidden-ssid-group').addClass('hidden');
        $('#noaa-station-id-group').removeClass('hidden'); // Show NOAA Station ID field

        if (security === 'NONE') {
            return; // nothing to do
        }
        if (security === 'ENTERPRISE') {
            $('#identity-group').removeClass('hidden');
            $('#passphrase-group').removeClass('hidden');
            return;
        }
        if (security === 'HIDDEN') {
            $('#hidden-ssid-group').removeClass('hidden');
            // fall through
        }
        // otherwise security is HIDDEN, WEP, WPA, or WPA2 which need password
        $('#passphrase-group').removeClass('hidden');
    }

    $('#ssid-select').change(showHideFormFields);

    $.get("/regcode", function(data){
        if(data.length !== 0){
            $('#regcode').val(data);
        } else { 
            $('.reg-row').hide(); // no reg code, so hide that part of the UI
	}
    });

    $.get("/networks", function(data){
        if(data.length === 0){
            $('.before-submit').hide();
            $('#no-networks-message').removeClass('hidden');
        } else {
            networks = JSON.parse(data);
            $.each(networks, function(i, val){
                $('#ssid-select').append(
                    $('<option>')
                        .text(val.ssid)
                        .attr('val', val.ssid)
                        .attr('data-security', val.security.toUpperCase())
                );
            });

            jQuery.proxy(showHideFormFields, $('#ssid-select'))();
        }
    });

    $('#connect-form').submit(function(ev){
        $.post('/connect', $('#connect-form').serialize(), function(data){
            $('.before-submit').hide();
            $('#submit-message').removeClass('hidden');
        });
        ev.preventDefault();
    });

    $("#toggle-password").click(function () {
        var passwordInput = $("#password-input");
        var passwordFieldType = passwordInput.attr('type');
      
        // Toggle the password visibility
        if (passwordFieldType === 'password') {
            passwordInput.attr('type', 'text');
            $("#toggle-password").text('Hide Password');
        } else {
            passwordInput.attr('type', 'password');
            $("#toggle-password").text('Show Password');
        }
    });
});
