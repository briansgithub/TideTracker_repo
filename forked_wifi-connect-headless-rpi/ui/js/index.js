$(function(){
    var networks = undefined;

    function showHideFormFields() {
        var security = $(this).find(':selected').attr('data-security');
        // start off with all fields hidden
        $('#identity-group').addClass('hidden');
        $('#passphrase-group').addClass('hidden');
        $('#hidden-ssid-group').addClass('hidden');
        if(security === 'NONE') {
            updateWifiSubmitButton();
            return; // nothing to do
        }
        if(security === 'ENTERPRISE') {
            $('#identity-group').removeClass('hidden');
            $('#passphrase-group').removeClass('hidden');
            
            // Uncheck the no-password checkbox when switching networks
            $('#no-password-checkbox').prop('checked', false);
            $('#passphrase').prop('disabled', false);
            $('#passphrase').show();
            $('#showPasswordBtn').show();
            
            updateWifiSubmitButton();
            return;
        } 
        if(security === 'HIDDEN') {
            $('#hidden-ssid-group').removeClass('hidden');
            // fall through
        } 
        // otherwise security is HIDDEN, WEP, WPA, or WPA2 which need password
        $('#passphrase-group').removeClass('hidden');
        
        // Uncheck the no-password checkbox when switching networks
        $('#no-password-checkbox').prop('checked', false);
        $('#passphrase').prop('disabled', false);
        $('#passphrase').show();
        $('#showPasswordBtn').show();
        
        updateWifiSubmitButton();
    }

    function updateWifiSubmitButton() {
        var isPasswordGroupHidden = $('#passphrase-group').hasClass('hidden');
        var isNoPasswordChecked = $('#no-password-checkbox').is(':checked');
        var passwordValue = $('#passphrase').val().trim();
        
        if (isPasswordGroupHidden) {
            $('#wifiSubmitBtn').prop('disabled', false);
        } else {
            if (isNoPasswordChecked || passwordValue.length > 0) {
                $('#wifiSubmitBtn').prop('disabled', false);
            } else {
                $('#wifiSubmitBtn').prop('disabled', true);
            }
        }
    }

    $('#no-password-checkbox').change(function() {
        if ($(this).is(':checked')) {
            $('#passphrase').val('');
            $('#passphrase').hide();
            $('#showPasswordBtn').hide();
        } else {
            $('#passphrase').show();
            $('#showPasswordBtn').show();
        }
        updateWifiSubmitButton();
    });

    $('#passphrase').on('input', function() {
        updateWifiSubmitButton();
    });



    // Add an event listener to update the input field when the dropdown changes
    $('#noaa-station-dropdown').change(function () {
        var selectedOption = this.options[this.selectedIndex];
        $('#noaa-station').val(selectedOption.text);
    });



    $('#ssid-select').change(showHideFormFields);

    $.get("/regcode", function(data){
        if(data.length !== 0){
            $('#regcode').val(data);
        } else { 
            $('.reg-row').hide(); // no reg code, so hide that part of the UI
	}
    });

    $.get("/status", function(data){
        var status = JSON.parse(data);
        var ssid = status.ssid ? status.ssid : 'None';
        $('#wifi-status-ssid').text('Currently connected to: ' + ssid);
        if (status.has_internet) {
            $('#wifi-status-internet').text('Has internet access').css('color', 'green');
        } else {
            $('#wifi-status-internet').text('No internet access').css('color', 'red');
        }
    }).fail(function(){
        $('#wifi-status-ssid').text('Currently connected to: Unknown');
        $('#wifi-status-internet').text('Status unavailable').css('color', '#888');
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

    // Function to toggle password visibility
    function togglePasswordVisibility() {
        var passwordField = $('#passphrase');
        var passwordType = passwordField.attr('type');
        passwordField.attr('type', passwordType === 'password' ? 'text' : 'password');
    }

    // Event binding for the "Show password" button
    $('#showPasswordBtn').click(togglePasswordVisibility);

    $('#connect-form').submit(function(ev){
        ev.preventDefault();
        $.post('/connect', $('#connect-form').serialize(), function(data){
            $('.before-submit').hide();
            $('#submit-message').removeClass('hidden');
        });
    });

    $('#station-form').submit(function(ev){
        ev.preventDefault();
        
        // Get the selected text from the dropdown
        var selectedStationText = $('#noaa-station-dropdown option:selected').text();

        // Set the selected text to the hidden input
        $('#noaa-station').val(selectedStationText);

        $.post('/update_station', $('#station-form').serialize(), function(data){
            alert("Station updated successfully!");
        });
    });

    $('#exitBtn').click(function() {
        $('.before-submit').hide();
        $('#exit-message').removeClass('hidden');
        
        // Attempt to close the window. 
        // Note: Modern browsers and captive portals often block this unless the window was opened by script.
        window.open('', '_self', ''); 
        window.close();
    });
});
